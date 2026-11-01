/// <reference types="cypress" />

const nativeEnter = () => cy.then(async () => {
  for (const type of ['keyDown', 'keyUp']) {
    await Cypress.automation('remote:debugger:protocol', { command: 'Input.dispatchKeyEvent', params: {
      type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
      ...(type === 'keyDown' ? { text: '\r', unmodifiedText: '\r' } : {}),
    } });
  }
});

describe('revision confidence V1 reuses the complete review workflow', () => {
  let fixture: any;
  before(() => cy.readFile('.review-confidence-e2e.json', { log: false }).then((v) => { fixture = v; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`freezes, recovers, reviews and reworks exact revisions in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/review-batches/`, name = `Confidence ${locale}`;
      const rejected = scope.sources[0], approved = scope.sources[1], self = scope.sources[4];
      let saved: number, frozenHash: string, original: any, newer: number, key: any, command: any, posts = 0;
      let nativeRecoveryTrusted = false;
      const visit = (role: string) => {
        // Unmount the annotation editor before invalidating its server session.
        // Otherwise its pending task request can race logout during role changes.
        cy.visit('/user/account/personal-info');
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr', 'lang', locale);
      };
      const open = () => cy.get('[data-testid="open-review-batches"]', { timeout: 30000 }).click();
      const stage = (name: string) => cy.task('confidenceStage', `${locale}: ${name}`);
      visit('producer');
      cy.request({ url: `/api/projects/${scope.id}/review-confidence-inputs/${rejected.id}/`,
        qs: { profile_id: fixture.profile_id, profile_digest: fixture.profile_digest } }).then(({ body }) => {
        expect(body.input_hash).to.eq(rejected.body.evaluated_input_hash);
        expect(body.subject.revision).to.eq(1);
      });
      visit('manager');
      cy.request(`/api/submissions/${rejected.id}/`).then(({ body }) => { original = body; });
      open();
      cy.contains('label', zh ? '审核批次名称' : 'Batch name').find('input').type(name);
      cy.contains('label', zh ? '抽样比例' : 'Sample percentage').find('input').clear().type('50');
      cy.contains('label', zh ? '优先抽审低置信度修订' : 'Prioritize low confidence revisions').find('input').check();
      cy.contains('label', zh ? '置信来源与版本' : 'Confidence source and version').find('select')
        .select(`${fixture.profile_id}:${fixture.profile_digest}`);
      cy.contains('label', zh ? '低置信度阈值' : 'Low confidence threshold').find('input').should('have.value', '400000');
      cy.contains('button', zh ? '预览抽样' : 'Preview sampling').click();
      cy.get('[data-testid="review-priority-summary"]').should('contain.text', zh ? '低分：3' : 'Low: 3');
      cy.intercept({ method: 'POST', pathname: new RegExp(`^${base.replace(/\/$/, '')}/?$`) }, (req) => {
        posts += 1;
        if (posts === 1) {
          key = req.headers['idempotency-key']; command = req.body;
          req.continue((res) => {
            expect(res.statusCode).to.eq(201); saved = res.body.id; frozenHash = res.body.plan_hash;
            res.send({ statusCode: 503, body: { detail: 'Synthetic lost acknowledgement' } });
          });
        } else {
          expect(req.headers['idempotency-key']).to.eq(key); expect(req.body).to.deep.eq(command);
          req.continue((res) => { expect(res.statusCode).to.eq(200); expect(res.body.id).to.eq(saved); expect(res.body.replayed).to.eq(true); });
        }
      }).as('confidenceCreate');
      cy.contains('button', zh ? '创建／重试' : 'Create / retry').click();
      cy.wait('@confidenceCreate').its('response.statusCode').should('eq', 503);
      cy.get('[data-testid="review-priority-recover"]').should('be.focused').then(($button) => {
        $button[0].addEventListener('click', (event) => { nativeRecoveryTrusted = event.isTrusted; });
      });
      nativeEnter();
      cy.wait('@confidenceCreate').its('response.statusCode').should('eq', 200);
      cy.then(() => expect(nativeRecoveryTrusted, 'trusted native Enter activation').to.eq(true));
      cy.contains('h3', name).scrollIntoView().should('be.visible');
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.algorithm).to.eq('revision-confidence-ranked/v1'); expect(body.selected_count).to.eq(3);
        expect(body.priority_source.profile_digest).to.eq(fixture.profile_digest);
        expect(JSON.stringify(body)).not.to.contain('producer_namespace').and.not.to.contain('event_uuid');
      });
      cy.contains('button', zh ? '发起仲裁' : 'Start arbitration').should('not.exist');
      cy.viewport(390, 844);
      cy.get('[data-testid="review-confidence-source"]').scrollIntoView().should('be.visible');
      cy.get('[data-testid="review-batches"]').should(($root) => {
        expect($root[0].scrollWidth).to.be.at.most($root[0].clientWidth + 2);
      });
      cy.screenshot(`confidence-manager-${locale}`);
      cy.viewport(1280, 900);
      stage('Manager frozen creation recovered');
      visit('reviewer');
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.candidate_count).to.eq(4); expect(body.selected_count).to.eq(2);
        expect(body.items.some((i: any) => i.submission_id === self.id)).to.eq(false);
        expect(body.priority_source).to.eq(undefined); expect(JSON.stringify(body)).not.to.contain('confidence_score_units');
      });
      open(); cy.contains('button', name).click();
      cy.contains('button', zh ? '审核本批样本' : 'Review this sample').click();
      cy.get('[data-testid^="review-submission-"]').should('have.length', 2);
      cy.get(`[data-testid="review-submission-${rejected.id}"]`).click();
      cy.get('[data-testid="review-reject-reason"]').type('Check revision 核对修订');
      cy.get('[data-testid="review-reject"]').click();
      cy.get('[data-testid="review-approve"]').click();
      cy.get('[data-testid="review-queue-empty"]').scrollIntoView().should('be.visible');
      stage('Reviewer rejected and approved original samples');
      visit('a');
      stage('Annotator authenticated');
      cy.get('[data-testid="open-review-batches"]').should('not.exist');
      cy.visit(`/projects/${scope.id}/data?task=${rejected.task_id}`);
      cy.get(`[data-annotation-id="${rejected.annotation_id}"]`, { timeout: 30000 }).click();
      cy.window().should((win: any) => { expect(String(win.Htx?.annotationStore?.selected?.pk)).to.eq(String(rejected.annotation_id)); expect(win.Htx?.isLoading).to.eq(false); });
      stage('Original annotation editor ready');
      cy.get('#label-studio-dm input[name="B"]').check({ force: true });
      cy.intercept({ method: 'PATCH', pathname: /\/api\/annotations\/\d+\/?$/ }).as('resubmitConfidence');
      cy.get('[data-testid="bottombar-update-button"]').click();
      cy.wait('@resubmitConfidence').its('request.body.submit_for_review').should('eq', true);
      stage('New revision submitted through editor');
      cy.request(`/api/submissions/?project=${scope.id}`).then(({ body }) => {
        const row = body.find((s: any) => s.assignment === rejected.assignment_id && s.revision === 2);
        expect(row.status).to.eq('pending'); newer = row.id;
      });
      visit('reviewer'); open(); cy.contains('button', name).click();
      cy.contains('button', zh ? '返工相关' : 'Rework follow-up').click();
      cy.contains('button', zh ? '对照返工修订' : 'Compare rework revisions').click();
      cy.get('[data-testid="rework-original"]').should('contain.text', 'Check revision 核对修订');
      cy.contains(zh ? '标注结果已变化。' : 'Annotation results changed.').should('be.visible');
      cy.screenshot(`confidence-rework-${locale}`);
      cy.contains('button', zh ? '返回批次' : 'Back to batch').click();
      visit('reviewer'); cy.get('[data-testid="open-review-workspace"]').click();
      cy.then(() => cy.get(`[data-testid="review-submission-${newer}"]`).click());
      cy.get('[data-testid="review-approve"]').click();
      visit('manager');
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.plan_hash).to.eq(frozenHash); expect(body.candidate_count).to.eq(5);
        expect(body.items.find((i: any) => i.submission_id === rejected.id).status).to.eq('rejected');
      });
      cy.request(`/api/submissions/${rejected.id}/`).then(({ body }) => {
        expect(body.result_hash).to.eq(original.result_hash); expect(body.result_snapshot).to.deep.eq(original.result_snapshot);
      });
      visit('producer');
      const producerUrl = `/api/projects/${scope.id}/review-confidence-observations/`;
      let producerPosts = 0;
      cy.intercept({ method: 'POST', pathname: new RegExp(`^${producerUrl.replace(/\/$/, '')}/?$`) }, (req) => {
        producerPosts += 1;
        expect(req.headers['idempotency-key']).to.eq(rejected.event);
        expect(req.body).to.deep.eq(rejected.body);
        req.continue((res) => {
          expect(res.statusCode).to.eq(200); expect(res.body.replayed).to.eq(true);
          if (producerPosts === 1) res.send({ statusCode: 503, body: { detail: 'Synthetic producer lost ACK' } });
        });
      }).as('producerOriginal');
      cy.getCookie('csrftoken', { log: false }).then((cookie) => {
        const send = () => cy.window().then((win) => win.fetch(producerUrl, { method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': cookie!.value, 'Idempotency-Key': rejected.event },
          body: JSON.stringify(rejected.body) }));
        send().then((res) => expect(res.status).to.eq(503));
        cy.wait('@producerOriginal');
        send().then((res) => { expect(res.status).to.eq(200); return res.json(); }).then((receipt) => {
          expect(receipt.replayed).to.eq(true); expect(receipt.submission_id).to.eq(rejected.id);
        });
        cy.wait('@producerOriginal');
      });
      stage('Machine HTTP original event recovered after lost ACK and reviewed source');
      visit('manager');
      cy.request({ method: 'POST', url: producerUrl, body: rejected.body, headers: { 'Idempotency-Key': rejected.event }, failOnStatusCode: false })
        .its('status').should('eq', 403);
      cy.request(`/api/projects/${scope.id}/members/?limit=100`).then(({ body }) => {
        const member = (body.results ?? body).find((m: any) => m.user.id === fixture.users.reviewer.id);
        cy.request('DELETE', `/api/projects/${scope.id}/members/${member.id}/`);
      });
      visit('reviewer'); cy.get('[data-testid="open-review-batches"]').should('not.exist');
      cy.then(() => cy.request({ url: `${base}${saved}/`, failOnStatusCode: false })).its('status').should('be.oneOf', [403, 404]);
    });
  }
});
