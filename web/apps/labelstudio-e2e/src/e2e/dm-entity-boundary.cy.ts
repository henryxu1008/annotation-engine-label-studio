/// <reference types="cypress" />

describe('Existing text entity boundary handles', () => {
  let fixture: any;
  before(() => cy.readFile('.enterprise-e2e.json').then(data => { fixture = data; }));

  // Native mouse input is required: synthetic mousemove does not extend the
  // browser's text selection. Coordinates account for the Cypress AUT iframe.
  const resize = (side: 'left' | 'right', offset: number) => {
    cy.get(`.htx-highlight area.__resize_${side}`).should('be.visible').then($handle => {
      const handle = $handle[0];
      const win = handle.ownerDocument.defaultView!;
      const root = win.document.querySelector('.lsf-richtext__line')!;
      const walker = win.document.createTreeWalker(root, 4);
      let node: Node | null;
      let remaining = offset;
      let target: DOMRect | undefined;
      while ((node = walker.nextNode())) {
        const text = node.textContent || '';
        const points = Array.from(text);
        if (remaining <= points.length) {
          const range = win.document.createRange();
          range.setStart(node, points.slice(0, remaining).join('').length);
          range.collapse(true);
          target = range.getBoundingClientRect();
          break;
        }
        remaining -= points.length;
      }
      expect(target, 'visible target caret').to.exist;
      const frame = win.frameElement!.getBoundingClientRect();
      const scaleX = frame.width / win.innerWidth;
      const scaleY = frame.height / win.innerHeight;
      const box = handle.getBoundingClientRect();
      const from = { x: frame.left + (box.left + box.width / 2) * scaleX, y: frame.top + (box.top + box.height / 2) * scaleY };
      const to = { x: frame.left + target!.left * scaleX, y: frame.top + (target!.top + target!.height / 2) * scaleY };
      return (async () => {
        const mouse = (type: string, x: number, y: number, pressed = false) => Cypress.automation('remote:debugger:protocol', {
          command: 'Input.dispatchMouseEvent',
          params: { type, x, y, button: type === 'mouseMoved' && !pressed ? 'none' : 'left', buttons: pressed ? 1 : 0, clickCount: type === 'mouseMoved' ? 0 : 1 },
        });
        await mouse('mouseMoved', from.x, from.y);
        await mouse('mousePressed', from.x, from.y, true);
        for (let step = 1; step <= 8; step++) await mouse('mouseMoved', from.x + (to.x - from.x) * step / 8, from.y + (to.y - from.y) * step / 8, true);
        await mouse('mouseReleased', to.x, to.y);
      })();
    });
  };

  for (const locale of ['en-US', 'zh-CN']) {
    for (const boundary of ['zh-chars', 'en-spaces', 'zh-punctuation', 'en-punctuation']) {
      it(`adjusts ${boundary}, preserves its note and restores/submits in ${locale}`, () => {
        cy.task('createEnterpriseE2EUndoScope', { entityDraft: true, entityBoundary: boundary }).then((scope: any) => {
          const value = scope.boundary;
          const target = Array.from(value.text).slice(value.targetStart, value.targetEnd).join('');
          const check = (result: any[]) => {
            expect(result).to.have.length(2);
            expect(result.find(r => r.type === 'labels')).to.include({ id: 'entity-1', from_name: 'entity' });
            expect(result.find(r => r.type === 'labels').value).to.deep.eq({ start: value.targetStart, end: value.targetEnd, text: target, labels: ['Entity'] });
            expect(result.find(r => r.type === 'textarea')).to.include({ id: 'entity-1' });
            expect(result.find(r => r.type === 'textarea').value.text).to.deep.eq(['原始备注']);
          };
          cy.loginAs(fixture.users.annotator_a.email, fixture.password, '/user/account/personal-info');
          cy.get('[data-testid="language-preference-select"]').select(locale).should('have.value', locale).and('not.be.disabled');
          cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_id}`);
          cy.window().should((win: any) => {
            expect(win.Htx?.isLoading).to.eq(false);
            expect(win.Htx?.annotationStore?.selected?.history?.isFrozen).to.eq(false);
          });
          cy.get('.htx-highlight').click();
          resize('left', value.targetStart);
          resize('right', value.targetEnd);
          cy.window().should((win: any) => check(win.Htx.annotationStore.selected.serializeAnnotation()));
          cy.get('[aria-label="' + (locale === 'zh-CN' ? '撤销' : 'Undo') + '"]').click();
          cy.window().should((win: any) => expect(win.Htx.annotationStore.selected.serializeAnnotation().find((r: any) => r.type === 'labels').value.end).to.eq(value.end));
          cy.get('[aria-label="' + (locale === 'zh-CN' ? '重做' : 'Redo') + '"]').click();
          cy.window().should((win: any) => check(win.Htx.annotationStore.selected.serializeAnnotation()));
          cy.get('body').then($body => { if ($body.find('[data-testid="draft-save-retry"]').length) cy.get('[data-testid="draft-save-retry"]').click(); });
          cy.window({ timeout: 15000 }).should((win: any) => expect(win.Htx.annotationStore.selected.hasUnsavedDraft).to.eq(false));
          cy.request(`/api/tasks/${scope.task_id}/`).then(({ body }) => { check(body.drafts[0].result); expect(body.annotations).to.deep.eq([]); });
          cy.reload();
          cy.window().should((win: any) => check(win.Htx.annotationStore.selected.serializeAnnotation()));
          cy.get('[data-testid="draft-save-retry"]').should('not.exist');
          cy.intercept('POST', `**/api/tasks/${scope.task_id}/annotations*`).as('submitBoundary');
          cy.get('[data-testid="bottombar-submit-button"]').click();
          cy.wait('@submitBoundary').then(({ response }) => { expect(response!.statusCode).to.eq(201); check(response!.body.result); });
          cy.request(`/api/tasks/${scope.task_id}/`).then(({ body }) => { expect(body.drafts).to.deep.eq([]); check(body.annotations[0].result); });
        });
      });
    }
  }
});
