/// <reference types="cypress" />
describe('manager classification gold evidence', () => {
  let fixture: any;
  before(() => cy.readFile('.classification81-e2e.json', { log: false }).then((value) => { fixture = value; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`freezes and scores revisions with manager-only access in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale], sample = scope.cases.split;
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr', 'lang', locale);
      };
      const button = (en: string, cn: string) => cy.contains('button', zh ? cn : en);
      let reference: number;
      login('manager');
      cy.get('[data-testid="open-classification-gold"]', { timeout: 30000 }).click();
      cy.get('[data-testid="classification-gold"] input').type(String(sample.task));
      button('Load revisions', '加载修订').click();
      cy.get('[data-testid="classification-gold"] select').first().select(String(sample.sources[0]));
      cy.intercept({ method: 'POST', pathname: new RegExp(`/api/projects/${scope.id}/classification-disagreements/gold/?$`) }).as('createGold');
      button('Create reference version', '创建参考答案版本').click();
      cy.wait('@createGold').then(({ response }) => { expect(response?.statusCode).to.eq(201); reference = response!.body.id; });
      cy.then(() => cy.get('[data-testid="classification-gold"] select').last().select(String(reference)));
      cy.get('[data-testid="classification-gold"] select').first().select(String(sample.sources[1]));
      button('Evaluate revision', '评分修订').click();
      cy.wait('@createGold').then(({ response }) => {
        expect(response?.statusCode).to.eq(201);
        expect(response!.body.manifest.contents.result.matched_controls).to.eq(0);
      });
      cy.get('[data-testid="classification-gold"] pre').should('contain.text', 'Negative');
      for (const role of ['reviewer', 'a']) {
        login(role);
        cy.get('[data-testid="open-classification-gold"]').should('not.exist');
        cy.then(() => cy.request({ url: `/api/projects/${scope.id}/classification-disagreements/gold/${reference}/`,
          failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]));
      }
    });
  }
});
