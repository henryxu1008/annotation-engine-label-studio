/// <reference types="cypress" />

describe('frozen NER revision arbitration', () => {
  let fixture: any;
  before(() => cy.readFile('.ner-arbitration-e2e.json', { log: false }).then(v => { fixture = v; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`dispatches, adopts and recovers only frozen revisions in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/ner-arbitrations/`;
      const batches = `/api/projects/${scope.id}/ner-disagreements/batches/`;
      const arb = () => cy.get('[data-testid="ner-arbitrations"]');
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr', 'lang', locale);
      };
      const action = (en: string, cn: string) => arb().contains('button', zh ? cn : en);
      const transition = (operation: string) => cy.task('nerArbitrationFixture', { operation, locale });
      let batch: any, first: any, sourceTask: any, revokedTask: any, original: any, posts = 0, forbidden = 0;
      login('manager');
      cy.request('POST', batches + 'preview/', { name: 'NER arbitration '+locale,
        scope: { kind: 'tasks', task_ids: Object.values(scope.cases).map((c: any) => c.task) } }).then(p =>
        cy.request({ method: 'POST', url: batches, headers: { 'Idempotency-Key': crypto.randomUUID() },
          body: { preview_token: p.body.preview_token, expected_plan_hash: p.body.plan_hash } })).then(r => { batch = r.body; });
      cy.get('[data-testid="open-ner-batches"]').click();
      cy.get('[data-testid="ner-batches"]').contains('button', 'NER arbitration '+locale).click();
      cy.get('[data-testid="ner-batches"]').find(`[data-task="${scope.cases.empty_winner.task}"]`).click();
      cy.get('[data-testid="dispatch-ner-arbitration"]').click();
      arb().find('select').select(String(fixture.users.reviewer.id));
      action('Confirm dispatch', '确认派发').click();
      cy.get('[data-testid="arbitration-saved"]').should('be.visible');
      cy.request(base).then(r => { first = r.body.results.find((v: any) => v.task_id === scope.cases.empty_winner.task); });
      arb().find('textarea').type('取消后重新明确派发');
      action('Confirm cancellation', '确认取消').click();
      cy.get('[data-testid="arbitration-saved"]').should('be.visible');
      action('Back to queue', '返回队列').click();
      cy.get('[data-testid="dispatch-ner-arbitration"]').click();
      arb().find('select').select(String(fixture.users.reviewer.id));
      action('Confirm dispatch', '确认派发').click();
      cy.get('[data-testid="arbitration-saved"]').should('be.visible');
      cy.request(base).then(r => {
        first = r.body.results.find((v: any) => v.task_id === scope.cases.empty_winner.task);
        const dispatch = (kind: string) => cy.request({ method: 'POST', url: base,
          headers: { 'Idempotency-Key': crypto.randomUUID() }, body: { ner_batch_id: batch.id,
            expected_manifest_hash: batch.manifest_hash, task_id: scope.cases[kind].task, assignee_id: fixture.users.reviewer.id } });
        dispatch('source').then(r => { sourceTask = r.body; });
        dispatch('revocation').then(r => { revokedTask = r.body; });
      });
      login('a');
      cy.then(() => cy.request({ url: base+first.id+'/', failOnStatusCode: false })).its('status').should('be.oneOf', [403,404]);
      login('reviewer');
      cy.get('[data-testid="open-ner-arbitrations"]').click();
      cy.then(() => arb().find(`[data-arbitration="${first.id}"]`).click());
      cy.viewport(390, 844);
      cy.get('[data-testid="ner-left-text"]').should('contain.text', 'Alice');
      arb().should('contain.text', zh ? '0 个实体' : '0 entities');
      cy.intercept({ method: 'POST', pathname: new RegExp(`${base}\\d+/decision/?$`) }, req => {
        if (!req.url.includes(`${base}${first.id}/`)) { req.continue(); return; }
        posts++; const body = req.body, key = req.headers['idempotency-key'];
        if (posts === 1) { original = { body, key }; req.continue(res => res.send({ ...res.body,
          receipt: { ...res.body.receipt, key: '00000000-0000-4000-8000-000000000000' } })); }
        else { expect(body).to.deep.eq(original.body); expect(key).to.eq(original.key); req.continue(); }
      }).as('decision');
      cy.intercept({ pathname: new RegExp(`/api/projects/${scope.id}/(classification-arbitrations|review-batches|ner-disagreements/tasks)/`) }, req => { forbidden++; req.continue(); });
      cy.then(() => arb().find(`input[value="${scope.cases.empty_winner.right}"]`).check());
      arb().find('textarea').type('选择已有空实体修订');
      action('Adopt this frozen revision', '采纳此冻结修订').scrollIntoView().should('be.visible');
      cy.screenshot(`ner-arbitration-narrow-${locale}`, { capture: 'fullPage' });
      action('Adopt this frozen revision', '采纳此冻结修订').click(); cy.wait('@decision');
      action('Recover original command', '恢复原命令').should('be.visible');
      action('Refresh read-only view', '刷新只读视图').click();
      cy.get('[data-testid="arbitration-decision"]').should('contain.text', '选择已有空实体修订');
      cy.then(() => expect(posts).to.eq(1));
      action('Recover original command', '恢复原命令').click(); cy.wait('@decision').then(({ response }) => {
        expect(response!.statusCode).to.eq(200); expect(response!.body.replayed).to.eq(true);
      });
      cy.get('[data-testid="arbitration-saved"]').should('be.visible');
      cy.screenshot(`ner-arbitration-adopt-${locale}`, { capture: 'fullPage' });
      // The remaining transitions mutate only runner-owned synthetic fixtures.
      action('Back to arbitration queue', '返回仲裁队列').click();
      transition('corrupt_source');
      cy.then(() => arb().find(`[data-arbitration="${sourceTask.id}"]`).click());
      cy.get('[data-testid="arbitration-unavailable"]').should('be.visible');
      cy.get('[data-testid="ner-left-text"]').should('not.exist'); arb().find('input[type="radio"]').should('not.exist');
      transition('restore_source'); transition('new_revision');
      action('Refresh read-only view', '刷新只读视图').click();
      cy.get('[data-testid="ner-left-text"]').should('contain.text', 'Alice');
      cy.then(() => cy.request(base+sourceTask.id+'/')).then(r => {
        expect(r.body.inputs.map((p: any) => p.submission_id)).to.include(scope.cases.source.left);
      });
      action('Back to arbitration queue', '返回仲裁队列').click();
      cy.then(() => arb().find(`[data-arbitration="${revokedTask.id}"]`).click());
      cy.get('[data-testid="ner-left-text"]').scrollIntoView().should('be.visible'); transition('revoke_reviewer');
      action('Refresh read-only view', '刷新只读视图').click(); arb().find('[role="alert"]').should('be.visible');
      cy.get('[data-testid="ner-left-text"]').should('not.exist');
      cy.then(() => cy.request({ method: 'POST', url: base+revokedTask.id+'/decision/', failOnStatusCode: false,
        headers: { 'Idempotency-Key': crypto.randomUUID() }, body: { expected_version: 1,
          expected_input_set_hash: revokedTask.input_set_hash, winner_submission_id: scope.cases.revocation.left, reason: 'denied' } })).its('status').should('be.oneOf', [403,404]);
      cy.then(() => expect(forbidden).to.eq(0));
      cy.screenshot(`ner-arbitration-revoked-${locale}`, { capture: 'fullPage' });
    });
  }
});
