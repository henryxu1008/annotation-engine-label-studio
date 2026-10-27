/// <reference types="cypress" />
describe('durable frozen NER queue', () => {
  let fixture: any;
  before(() => cy.readFile('.ner-e2e.json', { log: false }).then(v => { fixture = v; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`freezes, recovers, filters and protects original revisions in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/ner-disagreements/batches/`;
      const ids = ['differences','new_revision','source_loss','empty','self'].map(k => scope.cases[k].task);
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr','lang',locale);
      };
      const root = () => cy.get('[data-testid="ner-batches"]');
      const button = (en: string, cn: string) => root().contains('button',zh ? cn : en);
      const createRequests: any[] = [];
      let batch: any, origin: any, frozen: any;
      login('manager');
      cy.get('[data-testid="open-ner-batches"]', {timeout:30000}).click();
      button('New comparison batch','新建比较批次').click();
      root().find('input').first().type('NER queue ' + locale);
      root().find('select').select('tasks');
      root().find('input').last().type(ids.join(','));
      button('Preview comparison','预览比较').click();
      cy.get('[data-testid="ner-batch-preview"]').should('contain.text',ids[0]);
      cy.intercept({method:'POST',pathname:base,times:1},req=>{
        createRequests.push({body:req.body,key:req.headers['idempotency-key']});
        req.continue(res=>{
          expect(res.statusCode).to.eq(201);batch=res.body;
          res.send({statusCode:502,body:{detail:'Synthetic lost acknowledgement'}});
        });
      }).as('lostNerBatchAck');
      button('Create frozen batch','创建冻结批次').click(); cy.wait('@lostNerBatchAck');
      button('Recover original request','恢复原请求').should('be.visible');
      button('Back to batches','返回批次列表').click();
      root().contains('button','NER queue ' + locale).should('be.visible');
      cy.get('[data-testid="ner-batch-refresh"]').click();
      root().contains('button','NER queue ' + locale).should('be.visible');
      cy.then(()=>expect(createRequests).to.have.length(1));
      cy.intercept({method:'POST',pathname:base,times:1},req=>{
        createRequests.push({body:req.body,key:req.headers['idempotency-key']});
        req.continue(res=>{ expect(res.statusCode).to.eq(200);expect(res.body.replayed).to.eq(true);expect(res.body.id).to.eq(batch.id); });
      }).as('recoverNerBatch');
      button('Recover original request','恢复原请求').click();cy.wait('@recoverNerBatch');
      root().find('[data-task]').should('have.length',4);
      cy.then(()=>{
        expect(createRequests).to.have.length(2);
        expect(createRequests[1]).to.deep.eq(createRequests[0]);
        expect(batch.plan_hash).to.eq(createRequests[0].body.expected_plan_hash);
      });
      cy.then(()=>cy.request(base).then(r=>expect(r.body.count).to.eq(1)));
      root().find('select').select('all');
      root().find('[data-task]').should('have.length',5);
      root().find(`[data-task="${scope.cases.differences.task}"]`).click();
      cy.get('[data-testid="ner-matrix"]').should('contain.text','50.00%').and('contain.text','66.67%');
      cy.get('[data-testid="ner-left-text"]').should('contain.text','😀 李');
      cy.get('[data-testid="ner-difference"]').should('contain.text',zh?'边界不同':'different boundaries');
      cy.screenshot(`ner-batch-${locale}`,{capture:'fullPage'});
      button('Back to queue','返回队列').click();
      root().find('select').should('have.value','all');
      root().find(`[data-task="${scope.cases.differences.task}"]`).should('be.focused');
      cy.then(()=>cy.request(`${base}${batch.id}/items/${scope.cases.new_revision.task}/`).then(r=>{ origin=r.body; }));
      cy.task('nerFixture',{locale,operation:'new_revision'});
      cy.then(()=>cy.request(`${base}${batch.id}/items/${scope.cases.new_revision.task}/`).then(r=>{
        expect(r.body.manifest_hash).to.eq(origin.manifest_hash);
        expect(r.body.calculated_at).to.eq(origin.calculated_at);
        expect(r.body.item.inputs).to.deep.eq(origin.item.inputs);
        expect(r.body.item.calculation_hash).to.eq(origin.item.calculation_hash);
      }));
      cy.request('POST',base+'preview/',{name:'Newest NER',scope:{kind:'tasks',task_ids:[scope.cases.new_revision.task]}}).then(r=>{
        frozen=r.body;
        cy.request({method:'POST',url:base,body:{preview_token:frozen.preview_token,expected_plan_hash:frozen.plan_hash},
          headers:{'Idempotency-Key':crypto.randomUUID()}}).then(created=>{
          cy.request(`${base}${created.body.id}/items/${scope.cases.new_revision.task}/`).then(item=>{
            const newest = item.body.item.inputs.find((p:any)=>p.submitter_id===fixture.users.a.id);
            expect(newest.revision).to.eq(2);
          });
        });
      });
      cy.request({method:'POST',url:base+'preview/',body:{name:'Too large',scope:{kind:'tasks',task_ids:[scope.cases.budget.task]}},
        failOnStatusCode:false}).its('status').should('eq',422);
      let entered=false, release:(()=>void)|undefined;
      cy.then(()=>{ cy.intercept({method:'GET',pathname:`${base}${batch.id}/items/${scope.cases.differences.task}/`,times:1},
        req=>req.continue(()=>{entered=true;return new Cypress.Promise<void>(done=>{release=done;});})).as('lateNerBatchRead'); });
      root().find(`[data-task="${scope.cases.differences.task}"]`).click();
      cy.wrap(null).should(()=>expect(entered).to.eq(true));
      cy.window().then(win=>win.dispatchEvent(new win.Event('blur')));
      cy.then(()=>release!());cy.wait('@lateNerBatchRead');
      cy.get('[data-testid="ner-left-text"]').should('not.exist');cy.get('[data-testid="ner-matrix"]').should('not.exist');
      cy.get('[data-testid="ner-batch-refresh"]').click();root().find('[data-task]').should('have.length',5);
      cy.task('nerFixture',{locale,operation:'delete_source'});
      root().find(`[data-task="${scope.cases.source_loss.task}"]`).click();
      cy.get('[data-testid="ner-unavailable"]').should('be.visible');
      cy.get('[data-testid="ner-left-text"]').should('not.exist');cy.get('[data-testid="ner-matrix"]').should('not.exist');
      cy.then(()=>cy.request(`${base}${batch.id}/`).then(r=>expect(r.body.visible_task_count).to.eq(5)));
      login('reviewer');cy.get('[data-testid="open-ner-batches"]',{timeout:30000}).click();
      root().contains('button','NER queue ' + locale).click();
      root().find('select').select('all');root().find('[data-task]').should('have.length',4);
      root().find(`[data-task="${scope.cases.self.task}"]`).should('not.exist');
      button('New comparison batch','新建比较批次').should('not.exist');
      cy.then(()=>cy.request(`${base}${batch.id}/`).then(r=>{
        expect(r.body.visibility).to.eq('visible_subset');expect(r.body).not.to.have.property('manifest_hash');
        expect(r.body.visible_task_count).to.eq(4);
      }));
      cy.then(()=>cy.request({url:`${base}${batch.id}/items/${scope.cases.self.task}/`,failOnStatusCode:false})).its('status').should('eq',404);
      cy.task('nerFixture',{locale,operation:'revoke_reviewer'});
      cy.get('[data-testid="ner-batch-refresh"]').click();
      root().find('[role="alert"]').should('be.visible');root().find('[data-task]').should('not.exist');
      login('a');cy.get('[data-testid="open-ner-batches"]').should('not.exist');
      cy.request({url:base,failOnStatusCode:false}).its('status').should('be.oneOf',[403,404]);
      cy.then(()=>cy.request({url:`${base}${batch.id}/items/${scope.cases.differences.task}/`,failOnStatusCode:false})).its('status').should('be.oneOf',[403,404]);
    });
  }
});
