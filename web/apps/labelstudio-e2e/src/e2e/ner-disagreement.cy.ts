/// <reference types="cypress" />
import { getCypressElementCoordinates } from 'cypress-real-events/getCypressElementCoordinates';

describe('read-only frozen NER comparison', () => {
  let fixture: any;
  before(() => cy.readFile('.ner-e2e.json', { log: false }).then(value => { fixture = value; }));
  const selectText = (target: string) => {
    cy.get('.lsf-richtext__container').first().should('be.visible').then($root => {
      const root = $root[0], win = root.ownerDocument.defaultView!;
      const walker = win.document.createTreeWalker(root, win.NodeFilter.SHOW_TEXT);
      let node: Node | null, range: Range | undefined;
      while ((node = walker.nextNode())) {
        const start = node.textContent?.indexOf(target) ?? -1;
        if (start >= 0) { range = win.document.createRange(); range.setStart(node, start); range.setEnd(node, start + target.length); break; }
      }
      expect(range, 'real editor text range').to.exist;
      const box = root.getBoundingClientRect(), selected = range!.getBoundingClientRect();
      const from = getCypressElementCoordinates($root, { x: selected.left - box.left + 0.2,
        y: selected.top - box.top + selected.height / 2 }, false);
      const to = getCypressElementCoordinates($root, { x: selected.right - box.left - 0.2,
        y: selected.top - box.top + selected.height / 2 }, false);
      return (async () => {
        const mouse = (type: string, x: number, y: number, pressed = false) => Cypress.automation('remote:debugger:protocol', {
          command: 'Input.dispatchMouseEvent', params: { type, x, y, button: type === 'mouseMoved' && !pressed ? 'none' : 'left',
            buttons: pressed ? 1 : 0, clickCount: type === 'mouseMoved' ? 0 : 1 },
        });
        await mouse('mouseMoved', from.x, from.y); await mouse('mousePressed', from.x, from.y, true);
        for (let i = 1; i <= 8; i++) await mouse('mouseMoved', from.x + (to.x - from.x) * i / 8, from.y, true);
        await mouse('mouseReleased', to.x, to.y);
      })();
    });
  };
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`keeps frozen identities, metrics, permissions and races in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale];
      const login = (role: string, path = `/projects/${scope.id}/data`) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(path); cy.get('html').should('have.attr', 'lang', locale);
      };
      const url = (name: string) => `/api/projects/${scope.id}/ner-disagreements/tasks/${scope.cases[name].task}/`;
      const open = (name: string) => {
        cy.get('[data-testid="open-ner-disagreement"]', { timeout: 30000 }).click();
        cy.get('[data-testid="ner-workspace"] input[type="number"]').clear().type(String(scope.cases[name].task));
        cy.get('[data-testid="ner-load"]').click();
        cy.get('[data-testid="ner-workspace"] input[type="checkbox"]').each(box => cy.wrap(box).check());
        cy.get('[data-testid="ner-compare"]').click();
      };
      login('manager'); open('differences');
      cy.get('[data-testid="ner-matrix"]').should('contain.text', '50.00%').and('contain.text', '66.67%');
      cy.get('[data-testid="ner-difference"]').should('contain.text', zh ? '边界不同' : 'different boundaries');
      cy.get('[data-testid="ner-left-text"]').should('contain.text', '😀 李');
      cy.get('[data-testid="ner-back-matrix"]').click(); cy.get('[data-testid="ner-detail"]').should('not.exist');
      const ids = Object.values(scope.cases.differences.sources).map(Number).sort((a, b) => a - b);
      cy.get(`[data-testid="ner-pair-${ids[0]}-${ids[2]}"]`).click();
      cy.get('[data-testid="ner-detail"]').should('contain.text', '#' + ids[2]);
      let lateEntered = false, release: (() => void) | undefined;
      cy.intercept({ method: 'GET', pathname: new RegExp(url('differences') + '?$'), times: 1 }, request => request.continue(() => {
        lateEntered = true; return new Cypress.Promise<void>(done => { release = done; });
      })).as('lateNer');
      cy.get('[data-testid="ner-read"]').click(); cy.wrap(null).should(() => expect(lateEntered).to.eq(true));
      cy.window().then(win => win.dispatchEvent(new win.Event('blur')));
      cy.then(() => release!()); cy.wait('@lateNer'); cy.get('[data-testid="ner-detail"]').should('not.exist');
      cy.get('[data-testid="ner-read"]').click(); cy.get('[data-testid="ner-detail"]').should('contain.text', '#' + ids[2]);
      cy.screenshot(`ner-comparison-${locale}`, { capture: 'fullPage' });
      let frozen: any;
      const read = (name: string) => cy.request({ url: url(name), qs: { submissions: Object.values(scope.cases[name].sources).join(',') } });
      read('new_revision').then(({ body }) => { frozen = body; });
      cy.task('nerFixture', { operation: 'new_revision', locale });
      cy.then(() => cy.request({ url: url('new_revision'), qs: { frozen: frozen.frozen } })).then(({ body }) => {
        expect(body.result_hash).to.eq(frozen.result_hash); expect(body.calculated_at).to.eq(frozen.calculated_at);
      });
      read('source_loss').then(({ body }) => { frozen = body; });
      cy.task('nerFixture', { operation: 'delete_source', locale });
      cy.then(() => cy.request({ url: url('source_loss'), qs: { frozen: frozen.frozen } })).then(({ body }) => {
        expect(body.input_count).to.eq(3); expect(body.total_pairs).to.eq(3); expect(body.selected_pair).to.eq(null);
        expect(body.pairs).to.eq(null); expect(body.result_hash).to.eq(null);
      });
      read('empty').then(({ body }) => { expect(body.pairs[0].f1).to.eq(null); expect(body.reason).to.eq('no_entities'); });
      cy.request({ url: url('budget'), qs: { submissions: Object.values(scope.cases.budget.sources).join(',') }, failOnStatusCode: false })
        .its('status').should('eq', 422);
      login('reviewer');
      cy.request(url('self') + 'sources/').then(({ body }) => {
        expect(body.sources.map((r: any) => r.submitted_by_id)).not.to.include(fixture.users.reviewer.id);
      });
      cy.request({ url: url('self'), qs: { submissions: Object.values(scope.cases.self.sources).join(',') }, failOnStatusCode: false })
        .its('status').should('eq', 404);
      open('differences'); cy.get('[data-testid="ner-detail"]').should('be.visible');
      cy.task('nerFixture', { operation: 'revoke_reviewer', locale });
      cy.get('[data-testid="ner-read"]').click(); cy.get('[data-testid="ner-workspace"] [role="alert"]').should('be.visible');
      cy.get('[data-testid="ner-left-text"]').should('not.exist');
      login('a'); cy.get('[data-testid="open-ner-disagreement"]').should('not.exist');
      cy.request({ url: url('differences'), qs: { submissions: ids.join(',') }, failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]);
    });

    it(`uses real editor Unicode/CRLF selection, draft reload and immutable submissions in ${locale}`, () => {
      const scope = fixture.projects[locale], task = scope.cases.producer;
      cy.loginAs(fixture.users.a.email, fixture.password, '/user/account/personal-info'); cy.setUserLocale(locale);
      cy.visit(`/projects/${scope.id}/data?task=${task.task}&assignment=${task.assignment_a}`);
      cy.get('.lsf-label').contains('PER').click(); selectText('😀');
      cy.window().should((win: any) => {
        const result = win.Htx.annotationStore.selected.serializeAnnotation();
        expect(result).to.have.length(1); expect(result[0].value).to.include({ start: 1, end: 2, text: '😀' });
      });
      selectText('尾');
      const check = (rows: any[]) => {
        expect(rows).to.have.length(2);
        expect(rows.map(r => r.value).sort((a, b) => a.start - b.start)).to.deep.eq([
          { start: 1, end: 2, text: '😀', labels: ['PER'] }, { start: 8, end: 9, text: '尾', labels: ['PER'] },
        ]);
      };
      cy.window({ timeout: 15000 }).should((win: any) => {
        check(win.Htx.annotationStore.selected.serializeAnnotation()); expect(win.Htx.annotationStore.selected.hasUnsavedDraft).to.eq(false);
      });
      cy.reload(); cy.window().should((win: any) => check(win.Htx.annotationStore.selected.serializeAnnotation()));
      cy.intercept('POST', `**/api/tasks/${task.task}/annotations*`).as('nerSubmit');
      cy.get('[data-testid="bottombar-submit-button"]').click(); cy.wait('@nerSubmit').then(({ response }) => {
        expect(response!.statusCode).to.eq(201); check(response!.body.result);
      });
      cy.get('[data-testid="bottombar-update-button"]').should('be.disabled');
      cy.loginAs(fixture.users.manager.email, fixture.password, '/user/account/personal-info');
      const base = `/api/projects/${scope.id}/ner-disagreements/tasks/${task.task}/`;
      cy.request(base + 'sources/').then(({ body }) => cy.request({ url: base, qs: { submissions: body.sources.map((s: any) => s.id).join(',') } }))
        .then(({ body }) => { expect(body.pairs[0].f1).to.deep.eq({ numerator: '1', denominator: '1' }); expect(body.selected_pair.text).to.eq('A😀李 e\u0301\r\n尾'); });
    });
  }
});
