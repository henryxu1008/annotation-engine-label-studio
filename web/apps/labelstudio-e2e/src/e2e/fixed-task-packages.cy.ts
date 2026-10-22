/// <reference types="cypress" />

describe('fixed packages reuse existing work without dispatch', () => {
  let fixture: any;
  before(() => cy.readFile('.enterprise-e2e.json').then((data) => { fixture = data; }));

  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`creates and reads manager, annotator and reviewer scopes in ${locale}`, () => {
      const zh = locale === 'zh-CN';
      const project = fixture.project_id;
      const path = `/projects/${project}/data`;
      const base = `/api/projects/${project}/task-packages/`;
      let packageId: number;
      let assignmentCount: number;
      const visit = (email: string) => {
        cy.loginAs(email, fixture.password, '/user/account/personal-info');
        cy.get('[data-testid="language-preference-select"]').select(locale);
        cy.get('html').should('have.attr', 'lang', locale);
        cy.visit(path);
        cy.get('[data-testid="open-task-packages"]', { timeout: 30000 }).should('be.visible').click();
      };

      visit(fixture.users.manager.email);
      cy.request(`/api/task-assignments/?project=${project}&task=${fixture.tasks.a.id}`).its('body').then((body) => {
        assignmentCount = (body.results ?? body).length;
      });
      const taskIds = [fixture.tasks.a.id, fixture.tasks.b.id, fixture.tasks.review.id, fixture.tasks.release.id];
      const name = `Fixed ${locale} ${Date.now()}`;
      cy.contains('label', zh ? '任务包名称' : 'Package name').find('input').type(name);
      cy.contains('label', zh ? '共享标注说明' : 'Shared instructions').find('textarea').type('建议规则 v0.1');
      cy.contains('label', zh ? '任务 ID（' : 'Task IDs (').find('textarea').clear().type(taskIds.join(', '));
      cy.contains('button', zh ? '预览固定清单' : 'Preview fixed selection').click();
      cy.contains(zh ? '固定任务数：4' : 'Fixed tasks: 4').should('be.visible');
      cy.intercept({ method: 'POST', pathname: /\/task-packages\/?$/ }).as('createPackage');
      cy.contains('button', zh ? '确认创建／重试' : 'Confirm creation / retry').click();
      cy.wait('@createPackage').then(({ request, response }) => {
        expect(response?.statusCode).to.eq(201);
        expect(request.headers['idempotency-key']).to.match(/^[\da-f-]{36}$/);
        packageId = response!.body.id;
      });
      cy.contains('h3', name).should('be.visible');
      cy.contains(zh ? '固定样本数' : 'Fixed tasks').should('be.visible');
      cy.contains('button', zh ? '刷新进度' : 'Refresh progress').click();
      cy.contains('h3', name).should('be.visible');
      cy.request(`/api/task-assignments/?project=${project}&task=${fixture.tasks.a.id}`).its('body').then((body) => {
        expect((body.results ?? body).length, 'package creation performs no dispatch').to.eq(assignmentCount);
      });

      visit(fixture.users.annotator_a.email);
      cy.contains('button', name).click();
      cy.get('[data-testid="task-packages"]').should('contain.text', zh ? '仅显示你当前有效的分配' : 'Only your active assignments');
      cy.get('[data-testid="task-packages"] table tbody tr td:first-child').each(($cell) => {
        expect($cell.text()).not.to.eq(String(fixture.tasks.b.id));
      });
      cy.get('[data-testid="task-packages"] a').first().should('have.attr', 'href').and('include', `/projects/${project}/data?task=`);
      cy.then(() => cy.request(`${base}${packageId}/`)).then(({ body, headers }) => {
        expect(headers['cache-control']).to.eq('private, no-store');
        expect(body.counts).not.to.have.property('fixed_tasks');
        expect(body.package).not.to.have.property('membership_hash');
      });
      cy.request({ method: 'POST', url: base, failOnStatusCode: false,
        headers: { 'Idempotency-Key': crypto.randomUUID() }, body: { name: 'forbidden', task_ids: taskIds },
      }).its('status').should('eq', 403);

      visit(fixture.users.reviewer.email);
      cy.contains('button', name).click();
      cy.get('[data-testid="task-packages"]').should('contain.text', zh ? '仅显示你可复核的' : 'Only pending, non-self submissions');
      cy.get('[data-testid="task-packages"] a').should('not.exist');
      cy.contains('button', zh ? '打开现有复核队列' : 'Open existing review queue').should('be.visible');
      cy.then(() => cy.request(`${base}${packageId}/`)).its('body').then((body) => {
        expect(body.role).to.eq('reviewer');
        expect(Object.keys(body.counts)).to.deep.eq(['pending_review']);
        expect(body.results.length).to.be.greaterThan(0);
      });
    });
  }
});
