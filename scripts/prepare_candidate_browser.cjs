// Compile the existing Cypress journeys without installing the entire frontend
// workspace. The runner uses the matching Cypress and TypeScript package versions.
// Usage: node scripts/prepare_candidate_browser.cjs CHECKOUT OUTPUT CONTAINER URL
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { createRequire } = require("node:module");
const [checkout, output, container, baseUrl] = process.argv.slice(2);
if (!checkout || !output || !container || !baseUrl?.startsWith("http://localhost:")) {
  throw new Error("Supply checkout, exclusive runner directory, container and loopback HTTP URL");
}
const requireRunner = createRequire(path.resolve(output, "package.json"));
const ts = requireRunner("typescript");
const source = path.join(checkout, "web/apps/labelstudio-e2e/src");
const receipts = [];
for (const directory of ["support", "e2e"]) {
  const destination = path.join(output, directory);
  fs.mkdirSync(destination, { recursive: true });
  for (const name of fs.readdirSync(path.join(source, directory))) {
    if (!name.endsWith(".ts") && !name.endsWith(".cjs")) continue;
    if (name.endsWith(".d.ts") || name.endsWith(".test.cjs")) continue;
    const input = fs.readFileSync(path.join(source, directory, name));
    const result = name.endsWith(".ts") ? ts.transpileModule(input.toString(), {
      compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
      fileName: name,
    }).outputText : input;
    const target = name.replace(/\.ts$/, ".js");
    fs.writeFileSync(path.join(destination, target), result);
    receipts.push({ source: `${directory}/${name}`, generated: `${directory}/${target}`,
      source_sha256: crypto.createHash("sha256").update(input).digest("hex"),
      generated_sha256: crypto.createHash("sha256").update(result).digest("hex") });
  }
}
const config = {
  video: false, screenshotsFolder: path.resolve(output, "../evidence/browser-screenshots"),
  e2e: { baseUrl, testIsolation: true, supportFile: "support/e2e.js", specPattern: "e2e/**/*.cy.js",
    injectDocumentDomain: true },
};
const code = `const {execFileSync}=require('node:child_process');
module.exports=${JSON.stringify(config, null, 2)};
module.exports.e2e.setupNodeEvents=(on, config)=>{
 const run=(args)=>execFileSync('docker',['exec',${JSON.stringify(container)},'/label-studio/.venv/bin/python','label_studio/manage.py',...args],{stdio:'pipe'});
 on('task',{
  createEnterpriseE2ESubmission({taskId,actor}){run(['create_enterprise_e2e_submission',String(taskId),actor]);return null;},
  setEnterpriseE2EAssignment({action,taskId,actor}){run(['set_enterprise_e2e_assignment',action,String(taskId),actor]);return null;},
  setEnterpriseE2EMember({actor,enabled,projectId}){run(['set_enterprise_e2e_member',actor,'--enabled',enabled?'true':'false',...(projectId?['--project-id',String(projectId)]:[])]);return null;}
 });return config;
};
`;
fs.writeFileSync(path.join(output, "cypress.config.cjs"), code);
fs.writeFileSync(path.join(output, "../evidence/browser-source-hashes.json"), JSON.stringify({
  cypress_version: requireRunner("cypress/package.json").version, typescript_version: ts.version,
  transport: "loopback HTTP; trusted-browser HTTPS acceptance remains separate", receipts,
}, null, 2));
console.log(JSON.stringify({ compiled_specs: receipts.filter(r => r.source.startsWith("e2e/")).length }));
