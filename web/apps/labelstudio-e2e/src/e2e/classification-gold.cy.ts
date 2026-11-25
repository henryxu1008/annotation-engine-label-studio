/// <reference types="cypress" />
describe('manager classification gold evidence', () => {
  let fixture: any, blindFixture: any;
  before(() => cy.readFile('.classification81-e2e.json', { log: false }).then((value) => { fixture = value; }));
  before(() => cy.readFile('.classification82-blind-e2e.json', { log: false }).then((value) => { blindFixture = value; }));
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
      cy.get('[data-testid="classification-blind-scores"]').within(() => {
        button('Load blind scores', '加载盲投评分').click();
        cy.contains('button', zh ? `评分 #${blindFixture.projects[locale].score_id}` : `Score #${blindFixture.projects[locale].score_id}`).click();
        cy.contains(zh ? '匹配的控件：0 / 1' : 'Matched controls: 0 of 1').should('be.visible');
        cy.get('pre').should('contain.text', 'Positive').and('contain.text', 'Negative');
      });
      for (const role of ['reviewer', 'a']) {
        login(role);
        cy.get('[data-testid="open-classification-gold"]').should('not.exist');
        cy.then(() => cy.request({ url: `/api/projects/${scope.id}/classification-disagreements/gold/${reference}/`,
          failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]));
        cy.request({ url: `/api/projects/${scope.id}/classification-disagreements/gold/blind-scores/`,
          failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]);
      }
    });
  }
});
