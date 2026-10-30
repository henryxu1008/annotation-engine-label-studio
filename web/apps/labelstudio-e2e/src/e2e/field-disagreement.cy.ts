describe('frozen static text field comparison', () => {
  let fixture: any;
  before(() => cy.readFile('.field-e2e.json', { log: false }).then(value => { fixture = value; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`compares exact fields and preserves permissions, frozen scope and late-read guards in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale];
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr', 'lang', locale);
      };
      const url = (name: string) => `/api/projects/${scope.id}/field-disagreements/tasks/${scope.cases[name].task}/`;
      const read = (name: string) => cy.request({ url: url(name), qs: { submissions: Object.values(scope.cases[name].sources).join(',') } });
      const open = (name: string) => {
        cy.get('[data-testid="open-field-disagreement"]', { timeout: 30000 }).click();
        cy.get('[data-testid="field-workspace"] input[type="number"]').clear().type(String(scope.cases[name].task));
        cy.get('[data-testid="field-load"]').click();
        cy.get('[data-testid="field-workspace"] input[type="checkbox"]').each(box => cy.wrap(box).check());
        cy.get('[data-testid="field-compare"]').click();
      };
      login('manager'); open('differences');
      cy.get('[data-testid="field-matrix"]').should('contain.text', '66.67%').and('contain.text', '0.00%');
      cy.get('[data-testid="field-difference"]').should('contain.text', zh ? '仅格式不同' : 'Format only');
      cy.get('[data-testid="field-detail"]').should('contain.text', '001').and('contain.text', '张三');
      // A response must prove its manifest contents, not merely repeat an old digest.
      cy.intercept({ method: 'GET', pathname: new RegExp(url('differences') + '?$'), times: 1 }, request => request.continue(response => {
        response.body.manifest.inputs[0].revision++;
      })).as('changedFieldManifest');
      cy.get('[data-testid="field-read"]').click(); cy.wait('@changedFieldManifest');
      cy.get('[data-testid="field-workspace"] [role="alert"]').should('be.visible');
      cy.get('[data-testid="field-detail"]').should('not.exist');
      cy.get('[data-testid="field-read"]').click();
      cy.get('[data-testid="field-detail"]').should('contain.text', '001');
      cy.get('[data-testid="field-back-matrix"]').click();
      const ids = Object.values(scope.cases.differences.sources).map(Number).sort((a, b) => a - b);
      cy.get(`[data-testid="field-pair-${ids[0]}-${ids[2]}"]`).click();
      cy.get('[data-testid="field-detail"]').should('contain.text', '#' + ids[2]);
      let entered = false, release: (() => void) | undefined;
      cy.intercept({ method: 'GET', pathname: new RegExp(url('differences') + '?$'), times: 1 }, request => request.continue(() => {
        entered = true; return new Cypress.Promise<void>(done => { release = done; });
      })).as('lateFields');
      cy.get('[data-testid="field-read"]').click(); cy.wrap(null).should(() => expect(entered).to.eq(true));
      cy.window().then(win => win.dispatchEvent(new win.Event('blur')));
      cy.then(() => release!()); cy.wait('@lateFields'); cy.get('[data-testid="field-detail"]').should('not.exist');
      cy.get('[data-testid="field-read"]').click(); cy.get('[data-testid="field-detail"]').should('contain.text', '#' + ids[2]);
      cy.screenshot(`field-comparison-${locale}`, { capture: 'fullPage' });
      read('empty').then(({ body }) => {
        expect(body.reason).to.eq('no_values'); expect(body.pairs[0].raw_rate).to.eq(null);
        expect(body.pairs[0].denominator).to.eq(0); expect(body.pairs[0].excluded_empty_fields).to.eq(3);
      });
      read('missing').then(({ body }) => { expect(body.reason).to.eq('missing_field'); expect(body.pairs).to.eq(null); });
      read('unsupported').then(({ body }) => { expect(body.reason).to.eq('unsupported_config'); expect(body.selected_pair).to.eq(null); });
      cy.request({ url: url('budget'), qs: { submissions: Object.values(scope.cases.budget.sources).join(',') }, failOnStatusCode: false })
        .its('status').should('eq', 422);
      let frozen: any;
      read('new_revision').then(({ body }) => { frozen = body; });
      cy.task('fieldFixture', { operation: 'new_revision', locale });
      cy.then(() => cy.request({ url: url('new_revision'), qs: { frozen: frozen.frozen } })).then(({ body }) => {
        expect(body.result_hash).to.eq(frozen.result_hash); expect(body.calculated_at).to.eq(frozen.calculated_at);
      });
      read('source_loss').then(({ body }) => { frozen = body; });
      cy.task('fieldFixture', { operation: 'delete_source', locale });
      cy.then(() => cy.request({ url: url('source_loss'), qs: { frozen: frozen.frozen } })).then(({ body }) => {
        expect(body.input_count).to.eq(3); expect(body.total_pairs).to.eq(3); expect(body.configured_fields).to.eq(3);
        expect(body.pairs).to.eq(null); expect(body.selected_pair).to.eq(null); expect(body.result_hash).to.eq(null);
      });
      cy.task('fieldFixture', { operation: 'restore_source', locale });
      cy.then(() => cy.request({ url: url('source_loss'), qs: { frozen: frozen.frozen } })).then(({ body }) => {
        expect(body.result_hash).to.eq(frozen.result_hash); expect(body.manifest_hash).to.eq(frozen.manifest_hash);
        expect(body.selected_pair).to.deep.eq(frozen.selected_pair);
      });
      login('reviewer');
      cy.request(url('self') + 'sources/').then(({ body }) => {
        expect(body.sources.map((r: any) => r.submitted_by_id)).not.to.include(fixture.users.reviewer.id);
      });
      cy.request({ url: url('self'), qs: { submissions: Object.values(scope.cases.self.sources).join(',') }, failOnStatusCode: false })
        .its('status').should('eq', 404);
      open('differences'); cy.get('[data-testid="field-detail"] h4').scrollIntoView().should('be.visible');
      cy.task('fieldFixture', { operation: 'revoke_reviewer', locale });
      cy.get('[data-testid="field-read"]').click(); cy.get('[data-testid="field-workspace"] [role="alert"]').should('be.visible');
      cy.get('[data-testid="field-detail"]').should('not.exist');
      login('a'); cy.get('[data-testid="open-field-disagreement"]').should('not.exist');
      cy.request({ url: url('differences'), qs: { submissions: ids.join(',') }, failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]);
    });
  }
});
