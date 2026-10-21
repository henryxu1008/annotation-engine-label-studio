/// <reference types="cypress" />

describe("login survives a tips timeout", () => {
  for (const locale of ["en-US", "zh-CN"]) {
    it(`aborts a slow tips request and preserves a usable login form in ${locale}`, () => {
      let tipsSignal: AbortSignal | undefined;
      cy.readFile(".enterprise-e2e.json").then((fixture) => {
        cy.clearCookies();
        cy.intercept("GET", "**/heidi-tips", {
          delay: 10000,
          statusCode: 200,
          body: {},
        });
        cy.clock(Date.now(), ["setTimeout", "clearTimeout"]);
        cy.visit("/user/login/?next=%2Fprojects", {
          onBeforeLoad(win) {
            win.localStorage.removeItem("heidi_live_tips_collection");
            win.localStorage.removeItem("heidi_live_tips_collection_fetched_at");
            const nativeFetch = win.fetch.bind(win);
            win.fetch = (resource, options) => {
              if (String(resource).includes("/heidi-tips")) tipsSignal = options?.signal ?? undefined;
              return nativeFetch(resource, options);
            };
          },
        });
        cy.get('[data-testid="anonymous-language-select"]').select(locale);
        cy.get("html").should("have.attr", "lang", locale);
        cy.get("#email").type(fixture.users.manager.email);
        cy.get("#password").type(fixture.password, { log: false });
        cy.window().should(() => expect(tipsSignal, "actual tips fetch signal").not.to.eq(undefined));
        cy.tick(5001);
        cy.window().should(() => expect(tipsSignal?.aborted, "native AbortController completed").to.eq(true));
        cy.get("#email").should("have.value", fixture.users.manager.email);
        cy.get('form button[type="submit"]').should("be.visible").and("not.be.disabled");
        cy.clock().invoke("restore");
        cy.get('form button[type="submit"]').click();
        cy.location("pathname").should("eq", "/projects");
        cy.request("/api/current-user/whoami").its("body.email").should("eq", fixture.users.manager.email);
      });
    });
  }
});
