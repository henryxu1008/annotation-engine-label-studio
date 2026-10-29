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
        fieldFixture({ operation, locale }: { operation: string; locale: string }) {
          if (!['new_revision', 'delete_source', 'restore_source', 'revoke_reviewer'].includes(operation)
              || !['en-US', 'zh-CN'].includes(locale)) throw new Error('Invalid synthetic transition');
          const localPython = process.env.FIELD_TEST_PYTHON;
          const args = ['label_studio/manage.py', 'seed_field_e2e', '--operation', operation, '--locale', locale];
          const output = execFileSync(localPython || 'poetry', localPython ? args : ['run', 'python', ...args], {
            cwd: resolve(__dirname, '../../..'), env: { ...process.env, TEST_ENVIRONMENT: '1' }, encoding: 'utf8',
          });
          return JSON.parse(output.trim().split(/\r?\n/).at(-1)!);
        },
      });
      return result;
    },
  },
});
