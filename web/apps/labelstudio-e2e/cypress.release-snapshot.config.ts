import { defineConfig } from "cypress";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export default defineConfig({
  video: false, viewportWidth: 1440, viewportHeight: 1100, defaultCommandTimeout: 30000,
  e2e: {
    baseUrl: process.env.RELEASE_SNAPSHOT_E2E_URL ?? "http://localhost:8080",
    supportFile: "src/support/e2e.ts",
    specPattern: "src/e2e/release-snapshot.cy.ts",
    setupNodeEvents(on) {
      on("task", {
        snapshotSHA256(binary: string) { return createHash("sha256").update(Buffer.from(binary, "binary")).digest("hex"); },
        setSnapshotManagerEnabled(enabled: boolean) {
          if (typeof enabled !== "boolean") throw new Error("Boolean fixture operation required");
          const fixture = JSON.parse(readFileSync(resolve(__dirname, ".release-snapshot-e2e.json"), "utf8"));
          const ids = [fixture.project_id, fixture.manager_membership_id, fixture.users.manager.id];
          if (!ids.every((id) => Number.isSafeInteger(id) && id > 0)) throw new Error("Invalid fixture IDs");
          const code = `import os; from projects.models import ProjectMember; assert os.environ.get('TEST_ENVIRONMENT')=='1'; q=ProjectMember.objects.filter(pk=${ids[1]}, project_id=${ids[0]}, user_id=${ids[2]}, user__email='issue79-manager@example.test', project__title='Issue79 approved NER export'); assert q.count()==1; q.update(enabled=${enabled ? "True" : "False"})`;
          const container = process.env.RELEASE_SNAPSHOT_E2E_CONTAINER;
          if (container && !/^ls-issue80-app-[a-z0-9-]+$/.test(container)) throw new Error("Only #80 disposable app containers are allowed");
          execFileSync(container ? "docker" : "poetry", container
            ? ["exec", container, "python", "manage.py", "shell", "-c", code]
            : ["run", "python", "label_studio/manage.py", "shell", "-c", code],
          { cwd: resolve(__dirname, "../../.."), stdio: "pipe", env: process.env });
          return null;
        },
      });
    },
  },
});
