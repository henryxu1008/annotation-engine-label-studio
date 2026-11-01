import { defineConfig } from 'cypress';
import base from './cypress.config';

export default defineConfig({ ...base, e2e: { ...base.e2e, specPattern: 'src/e2e/review-confidence.cy.ts',
  setupNodeEvents(on, config) {
    const result = base.e2e?.setupNodeEvents?.(on, config);
    on('task', { confidenceStage(value) { console.log('CONFIDENCE_STAGE', value); return null; } });
    return result;
  },
} });
