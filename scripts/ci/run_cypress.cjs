// Use the locked Cypress Module API; keep raw config, HTTP errors and cookies out of evidence.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

function failureIndices(result, call) {
  if (!result || !Array.isArray(result.runs) || result.runs.length !== 1) return null;
  const actualSpec = String(result.runs[0]?.spec?.absolute || '').replaceAll('\\', '/');
  if (!call || !actualSpec.endsWith('/' + call.spec) || result.browserName !== call.browser) return null;
  const tests = result.runs[0]?.tests;
  if (!Array.isArray(tests) || tests.length === 0 || tests.length > 500) return null;
  const failed = tests.flatMap((entry, index) => entry?.state === 'failed' ? [index + 1] : []);
  const basename = path.posix.basename(call.spec).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const location = new RegExp('(?:^|[\\s/\\\\(])' + basename + ':(\\d{1,4}):\\d{1,4}(?:\\D|$)', 'm');
  const messages = tests.filter((entry) => entry?.state === 'failed').map((entry) =>
    [entry.displayError, ...(entry.attempts || []).flatMap((a) => [a?.error?.message, a?.error?.stack])]
      .filter((part) => typeof part === 'string').join('\n'));
  const lines = messages.map((message) => {
    const match = message.match(location);
    const line = match ? Number(match[1]) : 0;
    return Number.isSafeInteger(line) && line >= 1 && line <= 5000 ? line : 0;
  });
  const categories = messages.map((message) => {
    if (/cy\.task\(/i.test(message)) return 'task';
    if (/cy\.request\(|status code.*(?:4\d\d|5\d\d)/i.test(message)) return 'request';
    if (/timed out|timeout/i.test(message)) {
      if (/expected (?:undefined|null|NaN) to (?:equal|eq)/i.test(message)) return 'timeout-value';
      if (/expected to find (?:element|content)/i.test(message)) return 'timeout-element';
      if (/cy\.(?:visit|request|wait)\(/i.test(message)) return 'timeout-network';
      return 'timeout';
    }
    if (/uncaught|unhandled/i.test(message)) return 'uncaught';
    if (/assertionerror|expected/i.test(message)) return 'assertion';
    return 'other';
  });
  return failed.length ? { schema: 'ui-cypress-failure-indices/v3', total: tests.length, failed, categories, lines } : null;
}

function summarize(result, call) {
  assert.ok(result && Array.isArray(result.runs) && result.runs.length === 1, 'One actual spec result is required');
  const run = result.runs[0];
  const actualSpec = String(run.spec?.absolute || '').replaceAll('\\', '/');
  assert.ok(actualSpec.endsWith('/' + call.spec), 'Actual spec mismatch');
  assert.equal(result.browserName, call.browser);
  assert.equal(result.totalFailed, 0);
  assert.equal(result.totalPending, 0);
  assert.equal(result.totalSkipped, 0);
  assert.ok(Number.isSafeInteger(result.totalTests) && result.totalTests > 0);
  assert.equal(result.totalTests, result.totalPassed);
  const env = result.config?.env;
  for (const key of ['loginLocale', 'fullFlowLocale']) {
    assert.equal(env?.[key], call.env[key], 'Actual locale configuration mismatch');
  }
  assert.equal(result.config?.video, true);
  const configFile = String(result.config?.configFile || '').replaceAll('\\', '/');
  assert.ok(configFile.endsWith('/' + call.config_file), 'Actual config mismatch');
  const tests = run.tests.map((t) => {
    assert.equal(t.state, 'passed');
    assert.ok(Array.isArray(t.title) && t.title.every((s) => typeof s === 'string'));
    assert.ok(Array.isArray(t.attempts) && t.attempts.length === 1 && t.attempts[0].state === 'passed',
      'Unexpected retry/failed attempt');
    return { title: t.title.join(' / '), state: t.state, attempts: 1 };
  });
  assert.equal(tests.length, result.totalTests);
  if (call.language_evidence === 'in-spec-switch') {
    assert.equal(call.spec, 'apps/labelstudio-e2e/src/e2e/collaboration-locale.cy.ts');
    const required = [
      'keeps member role codes stable while translating the live roster',
      'preserves selected assignee and assignment version when switching inside its modal',
      'keeps the typed rejection reason and exact pending hash through a modal switch',
      'releases only the selected approved revision after a live language change',
    ];
    assert.equal(tests.length, required.length);
    for (const title of required) assert.ok(tests.some((t) => t.title.endsWith(' / ' + title)));
  } else if (call.languages.length === 2) {
    for (const locale of call.languages) {
      assert.ok(tests.some((t) => t.title.includes(locale)), 'Missing language cases: ' + locale);
    }
  }
  return { schema: 'ui-test-report/v1', kind: 'cypress', call, actual_spec: call.spec,
    actual_config: call.config_file, browser: result.browserName, tests,
    passed: result.totalPassed, failed: 0, pending: 0, skipped: 0,
    duration_ms: result.totalDuration, languages: call.languages };
}

async function main() {
  const [input, output, diagnostic] = process.argv.slice(2);
  const call = JSON.parse(fs.readFileSync(input, 'utf8'));
  // This tool is never a Docker/container client, including on a developer machine.
  assert.ok(!process.env.ENTERPRISE_E2E_CONTAINER, 'Container mode is forbidden');
  const root = path.resolve(__dirname, '../..');
  const web = path.join(root, 'web');
  const cypress = require(path.join(web, 'node_modules/cypress'));
  const result = await cypress.run({ project: path.join(web, 'apps/labelstudio-e2e'),
    configFile: path.join(web, 'apps/labelstudio-e2e', call.config_file),
    spec: path.join(web, call.spec), browser: call.browser, env: call.env,
    config: { baseUrl: process.env.UI_BASE_URL || 'http://localhost:8080', video: true },
  });
  // Only bounded numeric positions leave the private runner. Never export
  // Cypress errors, test titles, configuration, URLs or browser output.
  const failure = failureIndices(result, call);
  if (failure && diagnostic) fs.writeFileSync(diagnostic, JSON.stringify(failure), { flag: 'wx' });
  const report = summarize(result, call);
  const raw = JSON.stringify(report);
  const { redactDiagnostic } = require('../../web/apps/labelstudio-e2e/src/support/failure-diagnostics.cjs');
  assert.equal(redactDiagnostic(raw), raw, 'Credential-bearing diagnostic');
  if (process.env.UI_SECRET_FILE) {
    const secrets = JSON.parse(fs.readFileSync(process.env.UI_SECRET_FILE, 'utf8'));
    assert.ok(Array.isArray(secrets) && secrets.every((s) => typeof s === 'string' && s.length >= 8));
    assert.ok(!secrets.some((s) => raw.includes(s)), 'Synthetic credential in structured result');
  }
  assert.ok(Buffer.byteLength(raw) <= 4 * 1024 * 1024, 'Structured report exceeds budget');
  fs.writeFileSync(output, raw, { flag: 'wx' });
}

module.exports = { summarize, failureIndices };
if (require.main === module) main().catch(() => {
  // Cypress itself prints sanitized diagnostic output; do not serialize raw result/config/errors here.
  console.error('UI_CYPRESS_RESULT_INVALID');
  process.exitCode = 1;
});
