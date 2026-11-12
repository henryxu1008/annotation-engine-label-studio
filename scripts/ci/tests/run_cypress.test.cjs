const assert = require('node:assert/strict');
const test = require('node:test');
const { summarize, failureIndices } = require('../run_cypress.cjs');

const call = { spec: 'apps/labelstudio-e2e/src/e2e/review-confidence.cy.ts',
  config_file: 'cypress.review-confidence.config.ts', env: {}, browser: 'electron',
  languages: ['en-US', 'zh-CN'], language_evidence: 'test-title' };
function result() {
  return { browserName: 'electron', totalFailed: 0, totalPending: 0, totalSkipped: 0,
    totalTests: 2, totalPassed: 2, totalDuration: 100,
    config: { configFile: 'C:/candidate/web/apps/labelstudio-e2e/cypress.review-confidence.config.ts',
      video: true, env: { password: 'never-in-public-report' } },
    runs: [{ spec: { absolute: 'C:/candidate/web/' + call.spec }, tests: ['en-US', 'zh-CN'].map((locale) => ({
      title: ['complete workflow', 'frozen source in ' + locale], state: 'passed', attempts: [{ state: 'passed' }],
      displayError: 'Cookie: private-diagnostic-is-not-exported',
    })) }],
  };
}

test('Module API summary binds actual source, config, browser and language cases', () => {
  const summary = summarize(result(), call);
  assert.equal(summary.passed, 2);
  assert.deepEqual(summary.languages, ['en-US', 'zh-CN']);
  assert.ok(!JSON.stringify(summary).includes('never-in-public-report'));
  assert.ok(!JSON.stringify(summary).includes('private-diagnostic'));
});
test('failure diagnostics expose only bounded numeric test positions', () => {
  const value = result();
  value.totalFailed = 1;
  value.runs[0].tests[1].state = 'failed';
  const diagnostic = failureIndices(value, call);
  assert.deepEqual(diagnostic, { schema: 'ui-cypress-failure-indices/v2', total: 2, failed: [2], categories: ['other'] });
  assert.ok(!JSON.stringify(diagnostic).includes('private-diagnostic'));
  assert.equal(failureIndices(result(), call), null);
  const wrongSpec = result(); wrongSpec.runs[0].tests[0].state = 'failed'; wrongSpec.runs[0].spec.absolute = '/other/spec';
  assert.equal(failureIndices(wrongSpec, call), null);
  const tooMany = result(); tooMany.runs[0].tests = Array(501).fill({ state: 'failed' });
  assert.equal(failureIndices(tooMany, call), null);
  value.runs[0].tests[1].displayError = 'cy.task( failed; private payload';
  assert.deepEqual(failureIndices(value, call).categories, ['task']);
  assert.ok(!JSON.stringify(failureIndices(value, call)).includes('private payload'));
  value.runs[0].tests[1].displayError = 'Timed out retrying: expected undefined to equal false; private payload';
  assert.deepEqual(failureIndices(value, call).categories, ['timeout-value']);
  value.runs[0].tests[1].displayError = 'Timed out retrying: Expected to find element: private payload';
  assert.deepEqual(failureIndices(value, call).categories, ['timeout-element']);
});
test('Module API launch failure or incomplete result cannot be a passing report', () => {
  for (const value of [undefined, {}, { failures: 1 }, { runs: [] }, { ...result(), totalTests: 0 }]) {
    assert.throws(() => summarize(value, call));
  }
});
test('failed, pending, skipped, extra attempts and wrong locale cases reject', () => {
  const changes = [
    (v) => { v.totalFailed = 1; },
    (v) => { v.totalPending = 1; },
    (v) => { v.totalSkipped = 1; },
    (v) => { v.runs[0].tests[0].state = 'failed'; },
    (v) => { v.runs[0].tests[0].attempts.push({ state: 'passed' }); },
    (v) => { v.runs[0].tests[1].title = ['wrong locale']; },
    (v) => { v.totalTests = true; },
  ];
  for (const change of changes) {
    const value = result(); change(value);
    assert.throws(() => summarize(value, call));
  }
});
test('spec, config and default-browser substitutions reject', () => {
  const changes = [
    (v) => { v.runs[0].spec.absolute = '/wrong/spec.cy.ts'; },
    (v) => { v.config.configFile = '/wrong/cypress.config.ts'; },
    (v) => { v.browserName = 'chrome'; },
    (v) => { v.config.video = false; },
  ];
  for (const change of changes) {
    const value = result(); change(value);
    assert.throws(() => summarize(value, call));
  }
});
test('configured full-flow locale must match the actual Cypress configuration', () => {
  const full = { ...call, env: { fullFlowLocale: 'zh-CN' }, languages: ['zh-CN'], language_evidence: 'configured' };
  assert.throws(() => summarize(result(), full));
  const value = result(); value.config.env.fullFlowLocale = 'zh-CN';
  assert.equal(summarize(value, full).languages[0], 'zh-CN');
});
