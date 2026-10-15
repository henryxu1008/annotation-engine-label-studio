import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { readFileSync } from 'node:fs';
import { defineConfig } from 'cypress';
import { nxE2EPreset } from '@nx/cypress/plugins/cypress-preset';

export default defineConfig({
  e2e: {
    ...nxE2EPreset(__dirname),
    testIsolation: true,
    supportFile: 'src/support/e2e.ts',
    specPattern: 'src/e2e/**/*.cy.{js,jsx,ts,tsx}',
    setupNodeEvents(on, config) {
      const repoRoot = resolve(__dirname, '../../..');
      const runManagementCommand = (args: string[], capture = false) => {
        const container = process.env.ENTERPRISE_E2E_CONTAINER;
        return execFileSync(
          container ? 'docker' : 'poetry',
          container
            ? ['exec', container, '/deps/.venv/bin/python', 'label_studio/manage.py', ...args]
            : ['run', 'python', 'label_studio/manage.py', ...args],
          { cwd: repoRoot, stdio: capture ? 'pipe' : 'inherit', encoding: 'utf8', env: process.env },
        );
      };
      on('task', {
        createEnterpriseE2EUndoScope(options: { required?: boolean } = {}) {
          const fixture = JSON.parse(readFileSync(resolve(__dirname, '.enterprise-e2e.json'), 'utf8'));
          const ids = { project: fixture.project_id, manager: fixture.users.manager.id, annotator: fixture.users.annotator_a.id };
          if (!Object.values(ids).every((id) => Number.isSafeInteger(id) && Number(id) > 0)) throw new Error('Fixture IDs required');
          const output = runManagementCommand(['shell', '-c', `
import json
from django.db import transaction
from projects.models import Project, ProjectMember
from tasks.models import Task, TaskAssignment
ids = ${JSON.stringify(ids)}
with transaction.atomic():
    original = Project.objects.get(pk=ids['project'], created_by_id=ids['manager'], organization__title='Enterprise E2E Organization')
    assert original.members.filter(user_id=ids['annotator'], role=ProjectMember.Role.ANNOTATOR, enabled=True).exists()
    config = original.label_config
    if ${options.required === true ? 'True' : 'False'}:
        config = config.replace('<Choices ', '<Choices required="true" ', 1)
    project = Project.objects.create(title='Isolated editor browser regression', organization=original.organization, created_by_id=ids['manager'], is_published=True, enable_empty_annotation=True, label_config=config)
    ProjectMember.objects.create(project=project, user_id=ids['manager'], role=ProjectMember.Role.MANAGER)
    ProjectMember.objects.create(project=project, user_id=ids['annotator'], role=ProjectMember.Role.ANNOTATOR)
    task = Task.objects.create(project=project, data={'text': 'Independent undo regression.'}, overlap=1)
    TaskAssignment.objects.create(project=project, task=task, assignee_id=ids['annotator'], assigned_by_id=ids['manager'])
    project.summary.update_data_columns([task])
print('E2E_UNDO_SCOPE=' + json.dumps({'project_id': project.pk, 'task_id': task.pk}))
`], true);
          const result = output.split(/\r?\n/).find((line) => line.startsWith('E2E_UNDO_SCOPE='));
          if (!result) throw new Error('Independent Undo fixture was not created');
          return JSON.parse(result.slice('E2E_UNDO_SCOPE='.length));
        },
        createEnterpriseE2ESubmission({ taskId, actor }: { taskId: number; actor: string }) {
          runManagementCommand(['create_enterprise_e2e_submission', String(taskId), actor]);
          return null;
        },
        setEnterpriseE2EAssignment({
          action,
          taskId,
          actor,
        }: {
          action: "assign" | "cancel" | "clear";
          taskId: number;
          actor: string;
        }) {
          runManagementCommand(['set_enterprise_e2e_assignment', action, String(taskId), actor]);
          return null;
        },
        setEnterpriseE2EMember({
          actor,
          enabled,
          projectId,
        }: {
          actor: string;
          enabled: boolean;
          projectId?: number;
        }) {
          runManagementCommand([
            'set_enterprise_e2e_member', actor,
            '--enabled', enabled ? 'true' : 'false',
            ...(projectId ? ['--project-id', String(projectId)] : []),
          ]);
          return null;
        },
      });

      return config;
    },
    // Please ensure you use `cy.origin()` when navigating between domains and remove this option.
    // See https://docs.cypress.io/app/references/migration-guide#Changes-to-cyorigin
    injectDocumentDomain: true,
  },
});
