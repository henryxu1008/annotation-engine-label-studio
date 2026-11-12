/// <reference types="cypress" />
describe('revoked blind draft in a synthetic editor', () => {
  let fixture: any;
  before(() => cy.readFile('.classification82-blind-e2e.json', { log: false }).then((value) => { fixture = value; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`closes the stale draft editor in ${locale}`, () => {
      const scope = fixture.projects[locale];
      let handle: number;
      cy.loginAs(scope.draft_email, fixture.password, '/user/account/personal-info');
      cy.setUserLocale(locale);
      cy.visit(`/projects/${scope.id}/data`);
      cy.request(`/api/projects/${scope.id}/blind-work/`).then(({ body }) => {
        handle = body.results.find((row: any) => row.data.text === 'Synthetic 分类 split').id;
      });
      cy.window({ timeout: 30000 }).its('DM.SDK.blindWorkspace').should('be.a', 'function');
      cy.window().then((win: any) => {
        const current = win.DM.SDK, workspace = current.blindWorkspace;
        current.destroy();
        const root = win.document.createElement('div'); root.id = 'synthetic-blind-root'; win.document.body.appendChild(root);
        win.syntheticBlindManager = new win.DataManager({ root, projectId: scope.id, apiGateway: `${win.APP_SETTINGS.hostname}/api/dm`, apiVersion: 2,
          project: { id: scope.id }, settings: { blindProtocol: true }, blindWorkspace: workspace,
          interfaces: { labelingHeader: true, backButton: true }, labelStudio: {}, polling: false, locale });
      });
      cy.get('#synthetic-blind-root', { timeout: 30000 }).contains('Synthetic 分类 split').parent()
        .contains('button', locale === 'zh-CN' ? '打开标注' : 'Open annotation').click();
      cy.window({ timeout: 30000 }).should((win: any) => expect(win.syntheticBlindManager.store.taskStore.selected?.id).to.eq(handle));
      cy.request('/api/label_links/').its('status').should('eq', 200);
      cy.window().then((win: any) => {
        const wrapper = win.syntheticBlindManager.lsf;
        return wrapper.onSubmitDraft(null, wrapper.currentAnnotation);
      }).then((response: any) => expect(response.$meta.status).to.eq(404));
      cy.window({ timeout: 30000 }).should((win: any) => {
        expect(Boolean(win.syntheticBlindManager.store.taskStore.selected)).to.eq(false);
        expect(win.syntheticBlindManager.mode).to.eq('explorer');
      });
      cy.get('#synthetic-blind-root').should('not.contain.text', 'Synthetic 分类 split');
      cy.window().then((win: any) => win.syntheticBlindManager.destroy());
    });
  }
});