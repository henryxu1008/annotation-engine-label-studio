/// <reference types="cypress" />

declare global {
  namespace Cypress {
    interface Chainable {
      loginAs(email: string, password: string, nextPath?: string): Chainable<void>;
    }
  }
}

Cypress.Commands.add('loginAs', (email: string, password: string, nextPath = '/') => {
  // cy.session clears browser cookies before its setup callback. Log out while
  // the current actor's cookie is still present so Django invalidates that
  // server session before a different actor signs in.
  cy.visit('/logout/');
  cy.session(['ui-login', email, nextPath], () => {
    const loginPath = `/user/login/?next=${encodeURIComponent(nextPath)}`;
    cy.clearCookies();
    cy.visit(loginPath);
    cy.location('pathname', { timeout: 30000 }).should('eq', '/user/login/');
    cy.get('#email', { timeout: 30000 }).should('be.visible').clear().type(email);
    cy.get('#password', { timeout: 30000 }).should('be.visible').clear().type(password, { log: false });
    cy.get('form button[type="submit"]').click();
    cy.location('pathname', { timeout: 20000 }).should('not.eq', '/user/login/');
  }, {
    validate() {
      cy.request('/api/current-user/whoami').then((response) => {
        expect(response.status).to.eq(200);
        expect(response.body.email, 'authenticated actor').to.eq(email);
      });
    },
  });

  cy.visit(nextPath);
  if (nextPath === '/user/account/personal-info') {
    // Server authentication does not mean the frontend actor query has settled.
    // Account actions require the real actor rendered by AuthProvider.
    cy.get('input[type="email"]').should('have.value', email);
  }
  // Legacy enterprise specs assert English copy. Establish that test premise
  // explicitly when requested; bilingual specs choose their own locale.
  const loginLocale = Cypress.env('loginLocale');
  if (loginLocale) {
    if (loginLocale !== 'en-US' && loginLocale !== 'zh-CN') throw new Error(`Unsupported login locale: ${loginLocale}`);
    cy.request('/api/current-user/locale/').then(({ body }) => {
      cy.get('[data-testid="user-menu-trigger"]', { timeout: 30000 })
        .should('have.attr', 'title', email).click();
      cy.get('[data-testid="menu-language-select"]')
        .should('have.value', body.preference ?? 'auto').and('not.be.disabled').then(($select) => {
          if ($select.val() !== loginLocale) {
            cy.intercept({ method: 'PATCH', pathname: /^\/api\/current-user\/locale\/?$/ }).as('loginLocaleSaved');
            // A newly mounted workspace may refetch the actor after the first
            // readiness check. Query again so Cypress can retry actionability.
            cy.get('[data-testid="menu-language-select"]').should('be.enabled').select(loginLocale);
            cy.wait('@loginLocaleSaved').then(({ response }) => {
              expect(response?.statusCode).to.eq(200);
              expect(response?.body.preference).to.eq(loginLocale);
            });
          }
        });
    });
    cy.get('[data-testid="menu-language-select"]').should('have.value', loginLocale).and('not.be.disabled');
    cy.request('/api/current-user/locale/').its('body.preference').should('eq', loginLocale);
    cy.get('html').should('have.attr', 'lang', loginLocale);
    cy.get('[data-testid="user-menu-trigger"]').click();
  }
});

export {};
