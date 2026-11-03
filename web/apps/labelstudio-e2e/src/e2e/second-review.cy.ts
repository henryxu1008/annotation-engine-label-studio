/// <reference types="cypress" />
const nativeEnter = () => cy.then(async () => {
  for (const type of ['keyDown', 'keyUp']) await Cypress.automation('remote:debugger:protocol', {
    command: 'Input.dispatchKeyEvent', params: { type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
      ...(type === 'keyDown' ? { text: '\r', unmodifiedText: '\r' } : {}) },
  });
});
const nativeSpace = () => cy.then(async () => {
  for (const type of ['keyDown', 'keyUp']) await Cypress.automation('remote:debugger:protocol', {
    command: 'Input.dispatchKeyEvent', params: { type, key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 32,
      ...(type === 'keyDown' ? { text: ' ', unmodifiedText: ' ' } : {}) },
  });
});
describe('independent supervisory_only second review V1', () => {
  let fixture: any;
  before(() => cy.readFile('.second-review-e2e.json', { log: false }).then((v) => { fixture = v; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`dispatches, recovers, records opinions and preserves release boundaries in ${locale}`, () => {
      cy.viewport(1280, 900);
      const zh = locale === 'zh-CN', project = fixture.projects[locale];
      const api = `/api/projects/${project.id}/second-reviews/`;
      const source = project.sources[0], rejected = project.sources[1], cancelled = project.sources[2], historical = project.sources[3];
      let firstTask: string, secondTask: string, originalBody: any, originalKey: any, posts = 0, trusted = false;
      const visit = (role: string) => {
        cy.visit('/user/account/personal-info');
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${project.id}/data`);
        cy.get('html').should('have.attr', 'lang', locale);
      };
      const batch = () => {
        cy.get('[data-testid="open-review-batches"]', { timeout: 30000 }).click();
        cy.contains('button', `Supervision sample ${locale}`).click();
        cy.get('[data-testid="review-filter-selected-reviewed"]').click();
      };
      const choose = (id: number) => {
        cy.get(`[data-second-source="${id}"]`).click();
        cy.get('[data-testid="second-assignee"]').select(String(fixture.users.supervisor.id));
      };
      visit('manager'); batch(); choose(source.id);
      cy.intercept({ method: 'POST', pathname: new RegExp(`^${api.replace(/\/$/, '')}/?$`) }, (req) => {
        posts++;
        if (posts === 1) {
          originalBody = req.body; originalKey = req.headers['idempotency-key'];
          req.continue((res) => { expect(res.statusCode).to.eq(201); firstTask = res.body.receipt.task_id;
            res.send({ statusCode: 503, body: { detail: 'Synthetic lost acknowledgement' } }); });
        } else if (posts === 2) {
          expect(req.body).to.deep.eq(originalBody); expect(req.headers['idempotency-key']).to.eq(originalKey);
          req.continue((res) => { expect(res.statusCode).to.eq(200); expect(res.body.receipt.task_id).to.eq(firstTask); });
        }
      }).as('dispatch');
      cy.get('[data-testid="second-dispatch"]').click(); cy.wait('@dispatch').its('response.statusCode').should('eq', 503);
      cy.get('[data-testid="second-recover"]').should('be.focused').then(($button) => {
        $button[0].addEventListener('click', (event) => { trusted = event.isTrusted; });
      });
      nativeEnter(); cy.wait('@dispatch').its('response.statusCode').should('eq', 200);
      cy.then(() => expect(trusted, 'trusted native Enter recovery').to.eq(true));
      cy.get('[data-testid="review-result-snapshot"]').should('contain.text', 'Frozen 静态文本');
      cy.contains('button', zh ? '返回审核批次' : 'Return to review batches').click();
      cy.get(`[data-second-source="${source.id}"]`).should('be.focused');
      choose(rejected.id); cy.get('[data-testid="second-dispatch"]').click();
      cy.wait('@dispatch').then(({ response }) => { expect(response?.statusCode).to.eq(201); secondTask = response?.body.receipt.task_id; });
      cy.get('[data-testid="review-result-snapshot"]').scrollIntoView().should('be.visible');
      cy.contains('button', zh ? '返回审核批次' : 'Return to review batches').click();
      choose(cancelled.id); cy.get('[data-testid="second-dispatch"]').click(); cy.wait('@dispatch');
      cy.get('[data-testid="second-reason"]').type('Cancel 错派'); cy.get('[data-testid="second-cancel"]').click();
      cy.contains(zh ? '已取消，无监督意见' : 'Cancelled — no supervisory opinion').should('exist');
      cy.screenshot(`second-review-manager-${locale}`);
      visit('supervisor');
      cy.get('[data-testid="open-second-reviews"]', { timeout: 30000 }).click();
      cy.then(() => cy.get(`[data-second-task="${firstTask}"]`).click());
      cy.contains(zh ? '初审结论' : 'Initial review').should('be.visible');
      cy.get('[data-testid="second-reason"]').type('Concern 独立异议'); cy.get('[data-testid="second-concern"]').click();
      cy.contains(zh ? '复核有异议' : 'Concern recorded').scrollIntoView().should('be.visible');
      cy.contains('button', zh ? '返回受派队列' : 'Return to assigned queue').click();
      cy.then(() => cy.get(`[data-second-task="${secondTask}"]`).click());
      cy.get('[data-testid="second-confirm"]').click(); cy.contains(zh ? '复核无异议' : 'No concern recorded').scrollIntoView().should('be.visible');
      cy.contains('button', zh ? '返回受派队列' : 'Return to assigned queue').click();
      cy.get(`[data-second-task="${historical.second_task_id}"]`).click();
      cy.contains(zh ? '历史修订：' : 'Historical revision:').should('be.visible');
      cy.viewport(390, 844);
      cy.get('[data-testid="second-review-workspace"]').should(($root) => { expect($root[0].scrollWidth).to.be.at.most($root[0].clientWidth + 2); });
      cy.get('[data-testid="second-reason"]').type('Historical concern 历史修订异议');
      cy.get('[data-testid="second-concern"]').scrollIntoView().focus(); nativeEnter();
      cy.contains(zh ? '复核有异议' : 'Concern recorded').scrollIntoView().should('be.visible');
      cy.screenshot(`second-review-supervisor-${locale}`); cy.viewport(1280, 900);
      visit('manager');
      cy.request(`/api/submissions/${source.id}/release/`).its('status').should('eq', 200);
      cy.request({ url: `/api/submissions/${rejected.id}/release/`, failOnStatusCode: false }).its('status').should('eq', 400);
      cy.request(api).then(({ body }) => { expect(body.count).to.eq(4); expect(body.results.filter((r: any) => r.status === 'completed')).to.have.length(3); expect(body.results.filter((r: any) => r.status === 'cancelled')).to.have.length(1); });
      const followups = `/api/projects/${project.id}/second-review-followups/`;
      const openFollowups = () => {
        cy.get('[data-testid="open-second-reviews"]', { timeout: 30000 }).click();
        cy.get('[data-testid="open-concern-followups"]', { timeout: 30000 }).click();
      };
      let followupPosts = 0, followupBody: any, followupKey: any, followupId: string, successor: number;
      let trustedSpace = 0, trustedFollowupClicks = 0;
      openFollowups();
      cy.then(() => cy.get(`[data-concern-task="${firstTask}"]`).click());
      cy.get('[data-testid="followup-target"]').should('have.value', '');
      cy.get('[data-testid="followup-reason"]').type('Independent no-change decision 独立说明无需改动');
      cy.get('[data-testid="followup-confirm"]').check();
      cy.intercept({ method: 'POST', pathname: new RegExp(`^${followups.replace(/\/$/, '')}/?$`) }, req => {
        followupPosts++;
        if (followupPosts === 1) {
          followupBody = req.body; followupKey = req.headers['idempotency-key'];
          expect(req.body.disposition).to.eq('no_change'); expect(req.body.related_submission_id).to.eq(null);
          req.continue(res => { expect(res.statusCode).to.eq(201); followupId = res.body.receipt.followup_id;
            res.send({ statusCode: 503, body: { detail: 'Synthetic lost follow-up receipt' } }); });
        } else if (followupPosts === 2) {
          expect(req.body).to.deep.eq(followupBody); expect(req.headers['idempotency-key']).to.eq(followupKey);
          req.continue(res => { expect(res.statusCode).to.eq(200); expect(res.body.receipt.followup_id).to.eq(followupId); expect(res.body.replayed).to.eq(true); });
        } else {
          expect(req.body.disposition).to.eq('revision_linked'); expect(req.body.related_submission_id).to.eq(successor);
        }
      }).as('followupWrite');
      cy.get('[data-testid="followup-record"]').scrollIntoView().focus().then($button => {
        $button[0].addEventListener('keydown', event => { if (event.isTrusted && (event as KeyboardEvent).key === ' ') trustedSpace++; });
        $button[0].addEventListener('click', event => { if (event.isTrusted) trustedFollowupClicks++; });
      });
      nativeSpace(); cy.wait('@followupWrite').its('response.statusCode').should('eq', 503);
      cy.then(() => { expect(trustedSpace).to.eq(1); expect(trustedFollowupClicks).to.eq(1); });
      cy.get('[data-testid="followup-back-list"]').should('be.disabled');
      cy.get('[data-testid="followup-refresh"]').click();
      cy.get('[data-testid="followup-recover"]').should('be.focused');
      cy.then(() => expect(followupPosts).to.eq(1));
      nativeEnter(); cy.wait('@followupWrite').its('response.statusCode').should('eq', 200);
      cy.contains(zh ? '此处为记录时的冻结元数据' : 'Frozen metadata at record.').should('be.visible');
      cy.get('[data-testid="followup-back-list"]').click();
      cy.get(`[data-concern-task="${historical.second_task_id}"]`).click();
      cy.get('[data-testid="followup-target"]').should('have.value', '');
      let releaseReload: (() => void) | undefined;
      cy.intercept({ method: 'GET', times: 1, pathname: new RegExp(`^${followups.replace(/\/$/, '')}/?$`) }, req =>
        new Promise<void>(resolve => { releaseReload = () => { req.continue(); resolve(); }; })).as('followupReload');
      cy.get('[data-testid="followup-refresh"]').focus(); nativeEnter();
      cy.get('[data-testid="concern-followup-workspace"]').should('have.attr', 'aria-busy', 'true').find('h3').should('be.focused');
      cy.wrap(null).should(() => expect(releaseReload).to.be.a('function'));
      cy.then(() => releaseReload!()); cy.wait('@followupReload');
      cy.get('[data-testid="concern-followup-workspace"]').should('have.attr', 'aria-busy', 'false').find('h3').should('be.focused');
      cy.press(Cypress.Keyboard.Keys.TAB); cy.get('[data-testid="followup-refresh"]').should('be.focused');
      cy.get('[data-testid="followup-target"] option').then($options => {
        const value = [...$options].map(option => (option as HTMLOptionElement).value).find(Boolean)!;
        successor = Number(value); cy.get('[data-testid="followup-target"]').select(value);
      });
      cy.get('[data-testid="review-result-snapshot"]').should('have.length', 2);
      cy.contains(zh ? '所选后继修订当前审核状态：待初审' : 'Selected successor current review state: Pending initial review').scrollIntoView().should('be.visible');
      // Use the existing review API as the actual independent initial reviewer;
      // the follow-up interface cannot approve or change the successor itself.
      visit('initial');
      cy.then(() => cy.request('POST', `/api/submissions/${successor}/review/`, { decision: 'approved', reason: 'Successor initial review' }));
      visit('manager'); openFollowups();
      cy.get(`[data-concern-task="${historical.second_task_id}"]`).click();
      cy.then(() => cy.get('[data-testid="followup-target"]').select(String(successor)));
      cy.contains(zh ? '所选后继修订当前审核状态：已批准' : 'Selected successor current review state: Approved').scrollIntoView().should('be.visible');
      // The original owner uses the existing Editor write endpoint to submit
      // a later revision. Follow-up cannot create or approve that revision.
      visit(historical.owner);
      cy.request(`/api/projects/${project.id}/my-work/`).then(({ body }) => {
        const own = body.results.find((v: any) => v.assignment_id === historical.assignment_id);
        expect(own).to.exist;
        return cy.request(`/api/annotations/${historical.annotation_id}/`).then(({ body: annotation }) =>
          cy.request('PATCH', `/api/annotations/${historical.annotation_id}/`, {
            result: annotation.result, assignment_id: own.assignment_id,
            assignment_version: own.assignment_version, submit_for_review: true,
          }));
      });
      cy.request(`/api/projects/${project.id}/my-work/`).then(({ body }) => {
        successor = body.results.find((v: any) => v.assignment_id === historical.assignment_id).latest_submission_id;
      });
      visit('initial');
      cy.then(() => cy.request('POST', `/api/submissions/${successor}/review/`, { decision: 'rejected', reason: 'Independent successor rejection' }));
      visit('manager'); openFollowups();
      cy.get(`[data-concern-task="${historical.second_task_id}"]`).click();
      cy.then(() => cy.get('[data-testid="followup-target"]').select(String(successor)));
      cy.contains(zh ? '所选后继修订当前审核状态：已驳回' : 'Selected successor current review state: Rejected').scrollIntoView().should('be.visible');
      cy.get('[data-testid="followup-reason"]').type('Link existing reviewed successor 关联已有审核后继');
      cy.get('[data-testid="followup-confirm"]').check(); cy.get('[data-testid="followup-record"]').focus(); nativeEnter();
      cy.wait('@followupWrite').its('response.statusCode').should('eq', 201);
      cy.contains(zh ? '后继修订记录时状态：已驳回' : 'Successor status at record: Rejected').scrollIntoView().should('be.visible');
      cy.request(followups).then(({ body }) => { expect(body.count).to.eq(2); expect(body.results.every((r: any) => r.recorded?.version === 1)).to.eq(true); });
      cy.request(`/api/submissions/${source.id}/release/`).its('status').should('eq', 200);
      cy.request({ url: `/api/submissions/${rejected.id}/release/`, failOnStatusCode: false }).its('status').should('eq', 400);
      cy.screenshot(`concern-followup-${locale}`);
      visit('initial'); cy.request(api).its('body.count').should('eq', 0);
      cy.request({ url: followups, failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]);
      visit('a'); cy.get('[data-testid="open-second-reviews"]').should('not.exist');
      cy.request({ url: api, failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]);
    });
  }
});
