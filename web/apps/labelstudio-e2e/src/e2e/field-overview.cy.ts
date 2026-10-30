describe('single frozen field batch quality overview', () => {
  let fixture: any;
  before(() => cy.readFile('.field-overview-e2e.json', { log: false }).then(value => { fixture = value; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`replays task and annotator weighted field quality in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/field-disagreements/batches/`;
      const names = ['differences','format_only','agreement','empty','missing','self','new_revision','source_loss'];
      const tasks = [...names.map(name => scope.cases[name].task), ...scope.overview_tasks];
      const label = (en: string, cn: string) => zh ? cn : en;
      const button = (en: string, cn: string) => cy.contains('[data-testid="field-batches"] button', label(en,cn));
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr','lang',locale);
      };
      const read = () => cy.get('[data-testid="field-quality-read"]').click();
      const fraction = (n: number,d: number) => ({numerator:String(n),denominator:String(d)});
      let batch: number, frozen: string, hash: string, firstTime: string;
      cy.intercept({method:'GET',pathname:new RegExp('^'+base+'[0-9]+/quality-overview/?$')}).as('overview');
      login('manager');cy.get('[data-testid="open-field-batches"]').click();
      button('New comparison batch','新建比较批次').click();
      cy.get('[data-testid="field-batches"] input').first().type('Field quality '+locale);
      cy.get('[data-testid="field-batches"] select').select('tasks');
      cy.get('[data-testid="field-batches"] input').eq(1).type(tasks.join(','));
      button('Preview comparison','预览比较').click();
      button('Create frozen batch','创建冻结批次').click();
      read();
      cy.wait('@overview').then(({response})=>{
        expect(response!.statusCode).to.eq(200);
        const body=response!.body;
        batch=body.scope.batch_id;frozen=body.frozen;hash=body.result_hash;firstTime=body.summarized_at;
        expect(body.scope_counts.tasks).to.eq(34);
        expect(body.scope_counts.comparable_tasks).to.eq(32);
        expect(body.summary.raw_agreement).to.deep.eq(fraction(65,72));
        expect(body.summary.normalized_agreement).to.deep.eq(fraction(35,36));
        expect(body.summary.task_coverage).to.deep.eq(fraction(16,17));
        expect(body.summary.pair_coverage).to.deep.eq(fraction(22,25));
        expect(body.processing_cost.per_pass).to.deep.eq({normalized_values:219,field_equalities:282,
          aggregation_pair_visits:47,annotator_pair_updates:88});
        expect(body.processing_cost.scorer_calls).to.eq(0);
        expect(body.pagination.count).to.eq(56);
      });
      cy.get('[data-testid="field-quality-summary"]').should('contain.text','90.28%').and('contain.text','97.22%');
      cy.get('[data-testid="field-quality-next"]').click();
      cy.wait('@overview').then(({request,response})=>{
        expect(request.query.frozen).to.eq(frozen);expect(response!.body.result_hash).to.eq(hash);
        expect(response!.body.pagination.page).to.eq(2);expect(response!.body.annotators).to.have.length(6);
      });
      cy.get('[data-testid="field-quality-overview"] [data-quality-task]').first().click();
      cy.get('[data-testid="field-detail"]').should('be.visible');
      button('Back to queue','返回队列').click();
      cy.wait('@overview').then(({response})=>{
        expect(response!.body.pagination.page).to.eq(2);expect(response!.body.frozen).to.eq(frozen);
      });
      cy.get('[data-testid="field-quality-overview"] [data-quality-task]').should('have.length',6);
      cy.task('fieldOverviewFixture',{operation:'new_revision',locale});
      read();cy.wait('@overview').then(({response})=>{
        expect(response!.body.result_hash).to.eq(hash);expect(response!.body.summarized_at).to.eq(firstTime);
      });
      cy.task('fieldOverviewFixture',{operation:'delete_source',locale});
      read();cy.wait('@overview').then(({response})=>{
        expect(response!.body.source_status).to.eq('unavailable');expect(response!.body.scope_counts.tasks).to.eq(34);
        expect(response!.body.summary).to.eq(null);expect(response!.body.annotators).to.eq(null);
        expect(response!.body.result_hash).to.eq(null);expect(response!.body.frozen).to.eq(frozen);
      });
      cy.get('[data-testid="field-quality-summary"]').should('not.exist');
      cy.task('fieldOverviewFixture',{operation:'restore_source',locale});
      read();cy.wait('@overview').then(({response})=>expect(response!.body.result_hash).to.eq(hash));
      cy.task('fieldOverviewFixture',{operation:'grow_source',locale},{timeout:120000});
      read();cy.wait('@overview').then(({response})=>expect(response!.statusCode).to.eq(422));
      cy.get('[data-testid="field-quality-overview"] [role="alert"]').should('contain.text',label('budget','预算'));
      cy.get('[data-testid="field-quality-summary"]').should('not.exist');
      cy.task('fieldOverviewFixture',{operation:'restore_grown_source',locale},{timeout:120000});
      read();cy.wait('@overview').then(({response})=>{
        expect(response!.body.result_hash).to.eq(hash);expect(response!.body.frozen).to.eq(frozen);
      });
      // A late response cannot revive evidence after window blur.
      let entered=false,release:(()=>void)|undefined;
      cy.intercept({method:'GET',pathname:new RegExp('^'+base+'[0-9]+/quality-overview/?$'),times:1},
        request=>request.continue(()=>{entered=true;return new Cypress.Promise<void>(done=>{release=done;});})).as('late');
      read();cy.wrap(null).should(()=>expect(entered).to.eq(true));
      cy.window().then(win=>win.dispatchEvent(new win.Event('blur')));
      cy.then(()=>release!());cy.wait('@late');
      cy.get('[data-testid="field-quality-overview"]').should('not.exist');
      // The parent also clears its detail on blur; reauthorize it before replaying the retained receipt.
      cy.get('[data-testid="field-batch-refresh"]').click();
      cy.wait('@overview').then(({response})=>{
        expect(response!.body.frozen).to.eq(frozen);expect(response!.body.result_hash).to.eq(hash);
        expect(response!.body.summarized_at).to.eq(firstTime);expect(response!.body.pagination.page).to.eq(2);
      });
      cy.get('[data-testid="field-quality-summary"]').scrollIntoView().should('be.visible');
      cy.screenshot('field-quality-'+locale,{capture:'fullPage'});

      login('reviewer');cy.get('[data-testid="open-field-batches"]').click();
      button('Field quality '+locale,'Field quality '+locale).click();read();
      cy.wait('@overview').then(({response})=>{
        const body=response!.body;
        expect(body.scope.visibility).to.eq('visible_subset');expect(body.scope.reference_kind).to.eq('projection_hash');
        expect(body.scope_counts.tasks).to.eq(33);expect(body.pagination.count).to.eq(55);
        expect(body.annotators.some((p:any)=>p.actor_id===fixture.users.reviewer.id)).to.eq(false);
      });
      cy.task('fieldOverviewFixture',{operation:'revoke_reviewer',locale});
      read();cy.wait('@overview').then(({response})=>expect([401,403,404]).to.include(response!.statusCode));
      cy.get('[data-testid="field-quality-summary"]').should('not.exist');

      login('a');cy.get('[data-testid="open-field-batches"]').should('not.exist');
      cy.then(()=>cy.request({url:base+batch+'/quality-overview/',failOnStatusCode:false}))
        .then(response=>expect([403,404]).to.include(response.status));
    });
  }
});
