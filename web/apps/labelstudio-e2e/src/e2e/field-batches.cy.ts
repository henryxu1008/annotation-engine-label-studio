describe('durable frozen text field queues', () => {
  let fixture: any;
  before(() => cy.readFile('.field-batches-e2e.json', { log: false }).then(value => { fixture = value; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`creates, recovers and reads frozen field queues in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/field-disagreements/batches/`;
      const cases = ['differences','format_only','agreement','empty','self','new_revision','source_loss'];
      const tasks = cases.map(name => scope.cases[name].task);
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr','lang',locale);
      };
      const label = (en: string, cn: string) => zh ? cn : en;
      const button = (en: string, cn: string) => cy.contains('[data-testid="field-batches"] button',label(en,cn));
      const open = () => cy.get('[data-testid="open-field-batches"]',{timeout:30000}).click();
      const item = (name: string) => cy.get(`[data-testid="field-batches"] button[data-task="${scope.cases[name].task}"]`).click();
      const refresh = () => cy.get('[data-testid="field-batch-refresh"]').click();
      const all = () => cy.get('[data-testid="field-batches"] select').select('all');

      login('manager');open();
      button('New comparison batch','新建比较批次').click();
      cy.get('[data-testid="field-batches"] input').first().type('Fields '+locale);
      cy.get('[data-testid="field-batches"] select').select('tasks');
      cy.get('[data-testid="field-batches"] input').eq(1).type(tasks.join(','));
      button('Preview comparison','预览比较').click();
      let originalBody: any, originalKey: string, batch: number;
      cy.intercept({method:'POST',pathname:new RegExp(base+'$'),times:1},request=>{
        originalBody=request.body;originalKey=request.headers['idempotency-key'] as string;
        request.continue(response=>{
          batch=response.body.id;
          // The row really committed; an invalid acknowledgement must remain unknown.
          response.body.idempotency_key='00000000-0000-4000-8000-000000000001';
        });
      }).as('lostAck');
      button('Create frozen batch','创建冻结批次').click();cy.wait('@lostAck');
      button('Recover original request','恢复原请求').should('be.visible');
      cy.request(base).then(({body})=>{expect(body.count).to.eq(1);expect(body.results[0].id).to.eq(batch);});
      cy.intercept('POST',new RegExp(base+'$'),request=>{
        expect(request.body).to.deep.eq(originalBody);
        expect(request.headers['idempotency-key']).to.eq(originalKey);
      }).as('recover');
      button('Recover original request','恢复原请求').click();
      cy.wait('@recover').its('response.statusCode').should('eq',200);
      cy.get('[data-testid="field-batches"] table').should('be.visible');
      item('format_only');
      cy.get('[data-testid="field-detail"]').should('contain.text',label('Format only','仅格式不同'));
      cy.get('[data-testid="field-matrix"]').should('contain.text','100.00%').and('contain.text','0.00%');
      cy.get('[data-testid="field-batches"]').should('contain.text',label('Format differences','格式差异'));

      // Returning preserves the queue state, and reload finds a persisted batch.
      button('Back to queue','返回队列').click();
      cy.get('[data-testid="field-batches"] select').should('have.value','disagreement');
      button('Back to batches','返回批次列表').click();button('Fields '+locale,'Fields '+locale).click();
      item('differences');
      cy.get('[data-testid="field-detail"]').should('contain.text','001');
      button('Back to queue','返回队列').click();

      let entered=false,release:(()=>void)|undefined;
      cy.intercept({method:'GET',pathname:new RegExp(base+'[0-9]+/items/'+scope.cases.differences.task+'/$'),times:1},
        request=>request.continue(()=>{entered=true;return new Cypress.Promise<void>(done=>{release=done;});})).as('lateItem');
      item('differences');cy.wrap(null).should(()=>expect(entered).to.eq(true));
      cy.window().then(win=>win.dispatchEvent(new win.Event('blur')));
      cy.then(()=>release!());cy.wait('@lateItem');
      cy.get('[data-testid="field-detail"]').should('not.exist');
      refresh();item('differences');cy.get('[data-testid="field-detail"]').should('contain.text','001');
      button('Back to queue','返回队列').click();

      cy.intercept({method:'GET',pathname:new RegExp(base+'[0-9]+/items/'+scope.cases.differences.task+'/$'),times:1},
        request=>request.continue(response=>{response.body.item.evidence.cohort[0].source.revision++;})).as('badEvidence');
      item('differences');cy.wait('@badEvidence');
      cy.get('[data-testid="field-batches"] [role="alert"]').should('be.visible');
      cy.get('[data-testid="field-detail"]').should('not.exist');
      refresh();item('differences');cy.get('[data-testid="field-detail"]').should('contain.text','001');
      cy.screenshot('field-batches-'+locale,{capture:'fullPage'});

      let original: any;
      cy.then(()=>cy.request(base+batch+'/items/'+scope.cases.new_revision.task+'/')).then(({body})=>{original=body;});
      cy.task('fieldBatchFixture',{operation:'new_revision',locale});
      cy.then(()=>cy.request(base+batch+'/items/'+scope.cases.new_revision.task+'/')).then(({body})=>{
        expect(body.item.evidence_hash).to.eq(original.item.evidence_hash);
        expect(body.calculated_at).to.eq(original.calculated_at);
      });
      cy.then(()=>cy.request(base+batch+'/items/'+scope.cases.source_loss.task+'/')).then(({body})=>{original=body;});
      cy.task('fieldBatchFixture',{operation:'delete_source',locale});
      cy.then(()=>cy.request(base+batch+'/items/'+scope.cases.source_loss.task+'/')).then(({body})=>{
        expect(body.item.inputs).to.deep.eq([]);expect(body.item.comparable_pairs).to.eq(null);
        expect(body.item.evidence).to.eq(null);expect(body.item.comparison.selected_pair).to.eq(null);
        expect(body.item.total_pairs).to.eq(3);
      });
      refresh();all();item('source_loss');
      cy.get('[data-testid="field-unavailable"]').should('be.visible');
      cy.get('[data-testid="field-detail"]').should('not.exist');
      cy.task('fieldBatchFixture',{operation:'restore_source',locale});
      refresh();all();item('source_loss');cy.get('[data-testid="field-detail"]').should('contain.text','001');
      cy.then(()=>cy.request(base+batch+'/items/'+scope.cases.source_loss.task+'/')).then(({body})=>{
        expect(body.item.evidence_hash).to.eq(original.item.evidence_hash);
        expect(body.item.comparison).to.deep.eq(original.item.comparison);
      });
      cy.then(()=>cy.request(base+batch+'/items/'+scope.cases.empty.task+'/')).then(({body})=>{
        expect(body.item.reason).to.eq('no_values');
        expect(body.item.comparison.pairs[0].normalized_rate).to.eq(null);
      });

      login('reviewer');open();button('Fields '+locale,'Fields '+locale).click();
      cy.then(()=>cy.request({url:base+batch+'/items/',qs:{state:'all'}})).then(({body})=>{
        expect(body.visible_task_count).to.eq(tasks.length-1);
        expect(body).not.to.have.property('manifest_hash');expect(body).to.have.property('projection_hash');
      });
      cy.then(()=>cy.request({url:base+batch+'/items/'+scope.cases.self.task+'/',failOnStatusCode:false}))
        .its('status').should('eq',404);
      item('differences');cy.get('[data-testid="field-detail"]').should('contain.text','001');
      cy.task('fieldBatchFixture',{operation:'revoke_reviewer',locale});
      refresh();cy.get('[data-testid="field-batches"] [role="alert"]').should('be.visible');
      cy.get('[data-testid="field-detail"]').should('not.exist');
      login('a');cy.get('[data-testid="open-field-batches"]').should('not.exist');
      cy.then(()=>cy.request({url:base+batch+'/',failOnStatusCode:false})).its('status').should('be.oneOf',[403,404]);
    });
  }
});
