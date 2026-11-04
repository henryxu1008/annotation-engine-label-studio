/// <reference types="cypress" />
const nativeKey = (key: 'Enter' | ' ' | 'Tab') => cy.then(async () => {
  const code = key === ' ' ? 'Space' : key;
  const number = key === ' ' ? 32 : key === 'Tab' ? 9 : 13;
  for (const type of ['keyDown', 'keyUp']) await Cypress.automation('remote:debugger:protocol', {
    command: 'Input.dispatchKeyEvent', params: { type, key, code, windowsVirtualKeyCode: number, nativeVirtualKeyCode: number,
      ...(type === 'keyDown' && key !== 'Tab' ? { text: key === 'Enter' ? '\r' : ' ', unmodifiedText: key === 'Enter' ? '\r' : ' ' } : {}) },
  });
});
describe('fixed-redacted supervisory_only review audit', () => {
  let fixture: any;
  before(() => cy.readFile('.review-audit-e2e.json', { log: false }).then(v => { fixture = v; }));
  for (const locale of ['en-US', 'zh-CN']) {
    it(`freezes, recovers, downloads and preserves native focus in ${locale}`, () => {
      const input = fixture.supervision;
      const project = input.projects[locale];
      const base = `/api/projects/${project.id}/review-audit-packages/`;
      const ready = fixture.ready[locale];
      let posts = 0, originalBody: any, originalKey: any, captured: string, trustedSpace = 0;
      cy.viewport(1280, 900);
      cy.loginAs(input.users.manager.email, input.password, '/user/account/personal-info');
      cy.setUserLocale(locale);
      cy.visit(`/projects/${project.id}/data`);
      cy.get('[data-testid="open-review-batches"]', { timeout: 30000 }).click();
      cy.contains('button', `Supervision sample ${locale}`).click();
      cy.get('[data-testid="review-audit-open"]', { timeout: 30000 }).scrollIntoView().focus().then($button => {
        $button[0].addEventListener('click', e => { if (e.isTrusted) trustedSpace++; });
      });
      nativeKey(' ');
      cy.then(() => expect(trustedSpace, 'trusted native Space activation').to.eq(1));
      cy.get('[data-testid="review-audit"] h4').should('be.focused');
      cy.contains(locale === 'zh-CN' ? '固定脱敏' : 'Fixed redaction').should('exist');
      cy.intercept({ method: 'POST', pathname: new RegExp(`^${base}preview/?$`) }).as('preview');
      cy.get('[data-testid="review-audit-preview"]').should('be.enabled').focus(); nativeKey('Enter'); cy.wait('@preview').its('response.statusCode').should('eq', 200);
      cy.get('[data-testid="review-audit-create"]').should('be.disabled');
      cy.get('[data-testid="review-audit-confirm"]').focus(); nativeKey(' ');
      cy.get('[data-testid="review-audit-confirm"]').should('be.checked');
      cy.intercept({ method: 'POST', pathname: new RegExp(`^${base.replace(/\/$/, '')}/?$`) }, req => {
        posts++;
        if (posts === 1) {
          originalBody = req.body; originalKey = req.headers['idempotency-key'];
          req.continue(res => { expect(res.statusCode).to.eq(202); captured = res.body.package_id;
            res.send({ statusCode: 503, body: { detail: 'Synthetic lost acknowledgement' } }); });
        } else {
          expect(req.body).to.deep.eq(originalBody); expect(req.headers['idempotency-key']).to.eq(originalKey);
          req.continue(res => { expect(res.statusCode).to.eq(202); expect(res.body.package_id).to.eq(captured); });
        }
      }).as('capture');
      cy.get('[data-testid="review-audit-create"]').should('be.enabled').focus(); nativeKey(' '); cy.wait('@capture');
      cy.get('[data-testid="review-audit"] h4').should('be.focused');
      cy.get('[data-testid="review-audit-recover"]').should('be.enabled');
      nativeKey('Tab'); cy.get('[data-testid="review-audit-recover"]').should('be.focused');
      nativeKey('Enter'); cy.wait('@capture');
      cy.get('[data-testid="review-audit-status"]').should('exist');
      cy.get('[data-testid="review-audit-refresh"]').click();
      cy.get(`[data-audit-package="${ready}"]`).click();
      cy.get('[data-testid="review-audit-download"]').should('be.enabled');
      cy.intercept({ method: 'GET', pathname: new RegExp(`^${base}${ready}/artifacts/[^/]+/download/?$`) }).as('download');
      cy.get('[data-testid="review-audit-download"]').scrollIntoView().focus(); nativeKey('Enter');
      cy.wait('@download').then(({ response }) => {
        expect(response?.statusCode).to.eq(200);
        expect(response?.headers['content-type']).to.contain('application/zip');
        expect(response?.headers['cache-control']).to.eq('private, no-store');
      });
      cy.readFile(`${Cypress.config('downloadsFolder')}/review-audit-${ready}.zip`, 'binary', { timeout: 20000 }).then(data => {
        expect(data.slice(0, 2)).to.eq('PK');
        expect(data).to.contain('supervisory_only');
        expect(data).not.to.contain('Frozen 静态文本');
        expect(data).not.to.contain(input.users.manager.email);
      });
      cy.then(() => expect(posts).to.eq(2));
      // A synthetic 403 exercises browser retirement without mutating retained
      // users; real requester/creator revocation is covered by database/API tests.
      cy.intercept({ method: 'GET', pathname: new RegExp(`^${base.replace(/\/$/, '')}/?$`) }, { statusCode: 403, body: { display_context: { reason: 'AUDIT_FORBIDDEN' } } });
      cy.get('[data-testid="review-audit-refresh"]').focus(); nativeKey('Enter');
      cy.get('[data-testid="review-audit"]').should('not.exist');
      cy.get('[data-testid="review-batch-heading"]').should('be.focused');
      cy.screenshot(`review-audit-${locale}`);
    });
  }
});
