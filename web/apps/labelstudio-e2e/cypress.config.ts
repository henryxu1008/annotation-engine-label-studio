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
        createEnterpriseE2EUndoScope(options: { required?: boolean; delayedImage?: boolean; taskCount?: number; keyboard?: boolean; entityDraft?: boolean } = {}) {
          const taskCount = options.taskCount ?? 1;
          if (!Number.isSafeInteger(taskCount) || taskCount < 1 || taskCount > 3) throw new Error('Expected one to three isolated tasks');
          const fixture = JSON.parse(readFileSync(resolve(__dirname, '.enterprise-e2e.json'), 'utf8'));
          const ids = { project: fixture.project_id, manager: fixture.users.manager.id, annotator: fixture.users.annotator_a.id };
          if (!Object.values(ids).every((id) => Number.isSafeInteger(id) && Number(id) > 0)) throw new Error('Fixture IDs required');
          const output = runManagementCommand(['shell', '-c', `
import json
from django.db import transaction
from projects.models import Project, ProjectMember
from tasks.models import Task, TaskAssignment, AnnotationDraft
ids = ${JSON.stringify(ids)}
with transaction.atomic():
    original = Project.objects.get(pk=ids['project'], created_by_id=ids['manager'], organization__title='Enterprise E2E Organization')
    assert original.members.filter(user_id=ids['annotator'], role=ProjectMember.Role.ANNOTATOR, enabled=True).exists()
    config = original.label_config
    if ${options.keyboard === true ? 'True' : 'False'}:
        config = '<View><Text name="text" value="$text"/><Choices name="single" toName="text" choice="single"><Choice value="Positive"/><Choice value="Negative"/></Choices><Choices name="multiple" toName="text" choice="multiple"><Choice value="One"/><Choice value="Two"/></Choices><TextArea name="note" toName="text" rows="1" editable="true" placeholder="Single-line note"/><TextArea name="paragraph" toName="text" rows="3" editable="true" placeholder="Multiline note"/></View>'
    if ${options.entityDraft === true ? 'True' : 'False'}:
        config = '<View><Text name="text" value="$text"/><Labels name="entity" toName="text"><Label value="Entity"/></Labels><TextArea name="entity_note" toName="text" perRegion="true" rows="1" editable="true" placeholder="Entity note"/></View>'
    if ${options.required === true ? 'True' : 'False'}:
        config = config.replace('<Choices ', '<Choices required="true" ', 1)
    if ${options.delayedImage === true ? 'True' : 'False'}:
        config = config.replace('</View>', '<Image name="image" value="$image"/></View>')
    project = Project.objects.create(title='Isolated editor browser regression', organization=original.organization, created_by_id=ids['manager'], is_published=True, enable_empty_annotation=True, label_config=config)
    ProjectMember.objects.create(project=project, user_id=ids['manager'], role=ProjectMember.Role.MANAGER)
    ProjectMember.objects.create(project=project, user_id=ids['annotator'], role=ProjectMember.Role.ANNOTATOR)
    data = {'text': 'Independent undo regression.'}
    if ${options.delayedImage === true ? 'True' : 'False'}:
        data['image'] = '/ls76-delayed-image.png'
    tasks = []
    for index in range(${taskCount}):
        task_data = dict(data)
        if ${taskCount} > 1:
            task_data['text'] = 'Independent task ' + str(index + 1)
        task = Task.objects.create(project=project, data=task_data, overlap=1)
        assignment = TaskAssignment.objects.create(project=project, task=task, assignee_id=ids['annotator'], assigned_by_id=ids['manager'])
        if ${options.entityDraft === true ? 'True' : 'False'}:
            AnnotationDraft.objects.create(task=task, user_id=ids['annotator'], assignment=assignment, result=[{'id': 'entity-1', 'from_name': 'entity', 'to_name': 'text', 'type': 'labels', 'value': {'start': 0, 'end': 11, 'text': task_data['text'][:11], 'labels': ['Entity']}}, {'id': 'entity-1', 'from_name': 'entity_note', 'to_name': 'text', 'type': 'textarea', 'value': {'text': ['原始备注']}}])
        tasks.append(task)
    project.summary.update_data_columns(tasks)
print('E2E_UNDO_SCOPE=' + json.dumps({'project_id': project.pk, 'task_id': tasks[0].pk, 'task_ids': [task.pk for task in tasks]}))
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
