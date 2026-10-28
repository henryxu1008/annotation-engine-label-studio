import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { defineConfig } from 'cypress';
import base from './cypress.config';

export default defineConfig({
  ...base,
  e2e: {
    ...base.e2e,
    setupNodeEvents(on, config) {
      const result = base.e2e?.setupNodeEvents?.(on, config);
      on('task', {
        nerOverviewFixture({ operation, locale }: { operation: string; locale: string }) {
          if (!['new_revision', 'complete_missing', 'corrupt_source', 'restore_source', 'grow_source', 'shrink_source', 'revoke_reviewer'].includes(operation)
              || !['en-US', 'zh-CN'].includes(locale)) throw new Error('Invalid synthetic fixture transition');
          const output = execFileSync('poetry', ['run', 'python', 'label_studio/manage.py', 'seed_ner_overview_e2e',
            '--operation', operation, '--locale', locale], {
            cwd: resolve(__dirname, '../../..'), env: { ...process.env, TEST_ENVIRONMENT: '1' }, encoding: 'utf8',
          });
          return JSON.parse(output.trim().split(/\r?\n/).at(-1)!);
        },
      });
      return result;
    },
  },
});
