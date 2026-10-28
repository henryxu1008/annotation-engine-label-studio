/// <reference types="cypress" />
describe('single frozen NER batch quality overview', () => {
  let fixture: any;
  before(() => cy.readFile('.ner-overview-e2e.json', {log:false}).then(v => {fixture=v;}));
  for (const locale of ['en-US','zh-CN'] as const) {
    it(`preserves weighted F1, frozen evidence and authority in ${locale}`, () => {
      const zh=locale==='zh-CN',scope=fixture.projects[locale];
      const base=`/api/projects/${scope.id}/ner-disagreements/batches/`;
      const ids=['equal','mixed','empty','missing'].map(k=>scope.cases[k].task);
      const name='NER quality '+locale,pagerName='NER pages '+locale,reviewerName='NER visible '+locale;
      const root=()=>cy.get('[data-testid="ner-batches"]');
      const overview=()=>cy.get('[data-testid="ner-quality-overview"]');
      const button=(en:string,cn:string)=>root().contains('button',zh?cn:en);
      const login=(role:string)=>{
        cy.loginAs(fixture.users[role].email,fixture.password,'/user/account/personal-info');
        cy.setUserLocale(locale);cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr','lang',locale);
      };
      const transition=(operation:string)=>cy.task('nerOverviewFixture',{locale,operation});
      const create=(title:string,tasks:number[])=>cy.request('POST',base+'preview/',{name:title,scope:{kind:'tasks',task_ids:tasks}}).then(p=>
        cy.request({method:'POST',url:base,headers:{'Idempotency-Key':crypto.randomUUID()},
          body:{preview_token:p.body.preview_token,expected_plan_hash:p.body.plan_hash}}));
      let batch:any,origin:any,pagerOrigin:any,focusTask:number,reviewerBatch:any;
      let forbidden=0,readOnlyWrites=0;
      login('manager');
      cy.get('[data-testid="open-ner-batches"]',{timeout:30000}).click();
      button('New comparison batch','新建比较批次').click();
      root().find('input').first().type(name);root().find('select').select('tasks');root().find('input').last().type(ids.join(','));
      button('Preview comparison','预览比较').click();
      cy.get('[data-testid="ner-batch-preview"]').should('be.visible');button('Create frozen batch','创建冻结批次').click();
      cy.get('[data-testid="ner-quality-read"]').should('be.visible');
      cy.request(base).then(r=>{batch=r.body.results.find((b:any)=>b.name===name);expect(batch).to.exist;});
      cy.intercept('GET',new RegExp(`/api/projects/${scope.id}/classification-disagreements/.*/quality-overview`),req=>{forbidden++;req.continue();});
      cy.intercept({method:'GET',pathname:new RegExp(`${base}\\d+/quality-overview/?$`)}).as('overview');
      cy.get('[data-testid="ner-quality-read"]').click();cy.wait('@overview').then(({response:r})=>{
        expect(r!.statusCode).to.eq(200);origin=r!.body;
        expect(origin.summary.strict_f1).to.deep.eq({numerator:'7',denominator:'12'});
        expect(origin.scope_counts).to.include({tasks:4,comparable_tasks:2,total_pairs:6,comparable_pairs:4});
      });
      cy.get('[data-testid="ner-quality-summary"]').should('contain.text','58.33%').and('contain.text','66.67%');
      overview().find(`[data-quality-actor="${fixture.users.a.id}"]`).should('contain.text','62.50%');
      overview().should('contain.text',zh?'双方均无实体':'Both sides have no entities');
      cy.screenshot(`ner-quality-overview-${locale}`,{capture:'fullPage'});

      // A separate frozen batch proves real 52-person pagination without changing the main denominator.
      create(pagerName,scope.pager_tasks);
      create(reviewerName,[...ids,scope.cases.self.task]).then(r=>{reviewerBatch=r.body;});
      button('Back to batches','返回批次列表').click();root().contains('button',pagerName).click();
      cy.get('[data-testid="ner-quality-read"]').click();cy.wait('@overview').then(({response:r})=>{
        pagerOrigin=r!.body;expect(pagerOrigin.pagination.count).to.eq(52);expect(pagerOrigin.annotators).to.have.length(50);
      });
      cy.get('[data-testid="ner-quality-next"]').click();cy.wait('@overview').then(({response:r})=>{
        expect(r!.body.pagination.page).to.eq(2);expect(r!.body.annotators).to.have.length(2);
        expect(r!.body.result_hash).to.eq(pagerOrigin.result_hash);expect(r!.body.frozen).to.eq(pagerOrigin.frozen);
        focusTask=r!.body.annotators[0].task_ids[0];
      });
      cy.then(()=>overview().find(`[data-quality-task="${focusTask}"]`).first().click());
      cy.get('[data-testid="ner-matrix"]').should('contain.text','100.00%');
      button('Back to queue','返回队列').click();
      cy.wait('@overview');
      overview().find('[data-quality-actor]').should('have.length',2);
      cy.then(()=>overview().find(`[data-quality-task="${focusTask}"]`).first().should('be.focused'));
      root().find('select').should('have.value','disagreement');
      button('Back to batches','返回批次列表').click();root().contains('button',name).click();
      cy.get('[data-testid="ner-quality-read"]').click();cy.wait('@overview').then(({response:r})=>{origin=r!.body;});

      // Every remaining overview action is a GET; source fixture mutations are synthetic runner tasks.
      cy.intercept('POST',new RegExp(`${base.replace(/\/$/,'')}/?$`),req=>{readOnlyWrites++;req.continue();});
      transition('new_revision');transition('complete_missing');
      cy.get('[data-testid="ner-quality-read"]').click();cy.wait('@overview').then(({response:r})=>{
        expect(r!.body.frozen).to.eq(origin.frozen);expect(r!.body.result_hash).to.eq(origin.result_hash);
        expect(r!.body.summarized_at).to.eq(origin.summarized_at);expect(r!.body.scope_counts).to.deep.eq(origin.scope_counts);
      });
      transition('corrupt_source');cy.get('[data-testid="ner-quality-read"]').click();cy.wait('@overview').then(({response:r})=>{
        expect(r!.body.summary).to.eq(null);expect(r!.body.annotators).to.eq(null);
        expect(r!.body.receipt_verified).to.eq(false);expect(r!.body.scope_counts).to.deep.eq(origin.scope_counts);
      });
      overview().find('[data-quality-task]').should('not.exist');cy.get('[data-testid="ner-quality-summary"]').should('not.exist');
      transition('restore_source');cy.get('[data-testid="ner-quality-read"]').click();cy.wait('@overview').then(({response:r})=>{
        expect(r!.body.result_hash).to.eq(origin.result_hash);expect(r!.body.frozen).to.eq(origin.frozen);
      });
      transition('grow_source');cy.get('[data-testid="ner-quality-read"]').click();cy.wait('@overview').its('response.statusCode').should('eq',422);
      overview().find('[role="alert"]').should('contain.text',zh?'预算':'budget');overview().find('[data-quality-task]').should('not.exist');
      transition('shrink_source');cy.get('[data-testid="ner-quality-read"]').click();
      cy.wait('@overview');
      cy.get('[data-testid="ner-quality-summary"]').should('contain.text','58.33%');

      let entered=false,release:(()=>void)|undefined;
      cy.intercept({method:'GET',pathname:new RegExp(`${base}\\d+/quality-overview/?$`),times:1},req=>req.continue(()=>{
        entered=true;return new Cypress.Promise<void>(done=>{release=done;});
      })).as('lateOverview');
      cy.get('[data-testid="ner-quality-read"]').click();cy.wrap(null).should(()=>expect(entered).to.eq(true));
      cy.window().then(win=>win.dispatchEvent(new win.Event('blur')));cy.then(()=>release!());cy.wait('@lateOverview');
      cy.get('[data-testid="ner-quality-summary"]').should('not.exist');root().find('[data-quality-task]').should('not.exist');
      cy.then(()=>{expect(readOnlyWrites).to.eq(0);expect(forbidden).to.eq(0);});

      login('reviewer');cy.get('[data-testid="open-ner-batches"]',{timeout:30000}).click();root().contains('button',reviewerName).click();
      cy.get('[data-testid="ner-quality-read"]').click();
      cy.get('[data-testid="ner-quality-summary"]').should('contain.text','58.33%');
      overview().find(`[data-quality-task="${scope.cases.self.task}"]`).should('not.exist');
      cy.then(()=>cy.request(`${base}${reviewerBatch.id}/quality-overview/`)).then(r=>{
        expect(r.body.scope.reference_kind).to.eq('projection_hash');expect(r.body.scope_counts.tasks).to.eq(4);
        expect(r.body.annotators.map((a:any)=>a.actor_id)).not.to.include(fixture.users.reviewer.id);
      });
      transition('revoke_reviewer');cy.get('[data-testid="ner-quality-read"]').click();overview().find('[role="alert"]').should('be.visible');
      overview().find('[data-quality-task]').should('not.exist');cy.get('[data-testid="ner-quality-summary"]').should('not.exist');
      login('a');cy.get('[data-testid="open-ner-batches"]').should('not.exist');
      cy.then(()=>cy.request({url:`${base}${batch.id}/quality-overview/`,failOnStatusCode:false})).its('status').should('be.oneOf',[403,404]);
    });
  }
});
