export default {
  video: false,
  viewportWidth: 1440,
  viewportHeight: 1000,
  defaultCommandTimeout: 30000,
  e2e: {
    baseUrl: process.env.ISSUE86_E2E_URL ?? "http://127.0.0.1:8080",
    supportFile: "src/support/e2e.ts",
    specPattern: "src/e2e/package-claims-isolated.cy.ts",
  },
};
