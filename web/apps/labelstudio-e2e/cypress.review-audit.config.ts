import { defineConfig } from 'cypress';
import base from './cypress.config';
export default defineConfig({ ...base, e2e: { ...base.e2e, specPattern: 'src/e2e/review-audit.cy.ts' } });
