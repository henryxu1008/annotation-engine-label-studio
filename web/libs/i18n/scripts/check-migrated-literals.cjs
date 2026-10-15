const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { parse } = require("@babel/parser");

// All JSX/TSX source files changed in the locale migration, excluding tests.
// The checked-in manifest avoids relying on Git history in a shallow CI checkout.
const scopedFiles = require("./migrated-files.json");

const allowed = {
  "libs/editor/src/tools/Zoom.jsx": [
    { text: "pan", line: 19, reason: "Upstream automation selector; visible tool guidance uses the locale catalog." },
    { text: "zoom-in", line: 30, reason: "Upstream automation selector; visible tool guidance uses the locale catalog." },
    { text: "zoom-out", line: 58, reason: "Upstream automation selector; visible tool guidance uses the locale catalog." },
  ],
  "libs/editor/src/tools/Selection.js": [
    { text: "move-tool", line: 16, reason: "Upstream tool identifier; visible tool guidance uses the locale catalog." },
  ],
  "apps/labelstudio/src/pages/Home/HomePage.tsx": [
    { text: "Home", line: 220, reason: "Static route metadata; RoutesProvider translates the root breadcrumb." },
  ],
  "apps/labelstudio/src/pages/Projects/Projects.jsx": [
    { text: "Projects", line: 157, reason: "Static route metadata; RoutesProvider translates the projects breadcrumb." },
  ],
  "apps/labelstudio/src/pages/Settings/DangerZone.jsx": [
    { text: "Danger Zone", line: 245, reason: "Static route metadata; settings menu and breadcrumb translate by fixed child path." },
  ],
  "apps/labelstudio/src/pages/Settings/MembersSettings.jsx": [
    { text: "Members", line: 523, reason: "Static route metadata; settings menu and breadcrumb translate by fixed child path." },
  ],
  "apps/labelstudio/src/pages/ExportPage/ExportPage.jsx": [
    { text: "label-studio export", line: 345, reason: "Executable CLI syntax shown verbatim in timeout guidance." },
    { text: "--export-path=<output-path>", line: 345, reason: "Executable CLI argument syntax shown verbatim in timeout guidance." },
  ],
  "apps/labelstudio/src/pages/Settings/index.jsx": [
    { text: "Settings", line: 90, reason: "Static route metadata; RoutesProvider translates the settings breadcrumb by route path." },
  ],
  "apps/labelstudio/src/components/Menubar/Menubar.jsx": [
    { text: "API", line: 242, reason: "Public documentation product name and link label." },
    { text: "GitHub", line: 249, reason: "External service brand name." },
  ],
  "apps/labelstudio/src/pages/CreateProject/Import/Import.jsx": [
    { text: "PDF", line: 468, reason: "The PDF file-format acronym is identical in both locales." },
  ],
  "apps/labelstudio/src/pages/DataManager/DataManager.jsx": [
    { text: "English", line: 104, reason: "Language names are self-identifying choices." },
    { text: "简体中文", line: 105, reason: "Language names are self-identifying choices." },
  ],
  "libs/app-common/src/pages/AccountSettings/sections/Hotkeys/Import.tsx": [
    { text: '[{"id": 1, "section": "annotation-actions", "element": "button", "label": "Save", "key": "Ctrl+S"}]', line: 191, reason: "Example serialized hotkey payload; values must remain importable." },
  ],
  "libs/editor/src/components/App/App.jsx": [
    { text: "Task #", line: 191, reason: "Stable task-identity prefix required by the existing editor contract." },
  ],
  "libs/datamanager/src/components/MainView/DataView/empty-state/EmptyState.tsx": [
    { text: "Amazon S3", line: 141, reason: "External storage provider name." },
    { text: "Amazon S3", line: 142, reason: "External storage provider name." },
    { text: "Google Cloud Storage", line: 146, reason: "External storage provider name." },
    { text: "Google Cloud Storage", line: 147, reason: "External storage provider name." },
    { text: "Azure Blob Storage", line: 151, reason: "External storage provider name." },
    { text: "Azure Blob Storage", line: 152, reason: "External storage provider name." },
    { text: "Redis Storage", line: 156, reason: "External storage provider name." },
    { text: "Redis Storage", line: 157, reason: "External storage provider name." },
  ],
  "libs/app-common/src/pages/AccountSettings/sections/LanguagePreferences.tsx": [
    { text: "English", line: 26, reason: "Language names are self-identifying choices." },
    { text: "简体中文", line: 27, reason: "Language names are self-identifying choices." },
  ],
  "libs/app-common/src/pages/AccountSettings/AccountSettings.tsx": [
    { text: "My Account", line: 128, reason: "Static route metadata; RoutesProvider translates account breadcrumbs by path." },
  ],
  "libs/app-common/src/pages/AccountSettings/sections/Hotkeys/Help.tsx": [
    { text: "default", line: 91, reason: "Stable hotkey subgroup identifier; display labels are resolved separately." },
  ],
  "libs/datamanager/src/components/Filters/types/Number.jsx": [
    { text: "is between", line: 90, reason: "Operator metadata is translated by key in FilterOperation before rendering." },
    { text: "not between", line: 96, reason: "Operator metadata is translated by key in FilterOperation before rendering." },
  ],
  "libs/editor/src/components/BottomBar/buttons.tsx": [
    { text: "Reject", line: 87, reason: "The built-in Reject control translates its title at render time in ControlButton." },
    { text: "Reject annotation: [ Ctrl+Space ]", line: 91, reason: "The built-in Reject control translates its tooltip at render time in ControlButton." },
  ],
};

const humanAttribute = new Set(["aria-label", "ariaLabel", "title", "placeholder", "alt", "tooltip", "label", "body", "message", "okText", "cancelText"]);
const humanDefaultName = /(?:label|title|placeholder|tooltip|message|text)$/i;
const normalized = (text) => text.replace(/\s+/g, " ").trim();
const humanText = (text) => /[A-Za-z\u4e00-\u9fff]{2,}/u.test(text);

function scanSource(source) {
  const ast = parse(source, { sourceType: "unambiguous", plugins: ["jsx", "typescript"] });
  const literals = [];
  const bindings = new Map();
  const assignments = new Map();
  const collectBindings = (node) => {
    if (!node || typeof node !== "object") return;
    if (node.type === "VariableDeclarator" && node.id?.type === "Identifier" && node.init) {
      const definitions = bindings.get(node.id.name) ?? [];
      definitions.push(node);
      bindings.set(node.id.name, definitions);
    }
    if (node.type === "AssignmentExpression" && node.left?.type === "Identifier") {
      const updates = assignments.get(node.left.name) ?? [];
      updates.push(node);
      assignments.set(node.left.name, updates);
    }
    for (const [key, value] of Object.entries(node)) {
      if (["loc", "start", "end", "extra", "tokens", "comments"].includes(key)) continue;
      if (Array.isArray(value)) value.forEach(collectBindings);
      else if (value && typeof value === "object") collectBindings(value);
    }
  };
  collectBindings(ast);
  const resolving = new Set();
  const addLiteral = (value, line) => {
    const text = normalized(value);
    if (humanText(text)) literals.push({ text, line });
  };
  const visitDisplayExpression = (expression) => {
    if (!expression) return;
    switch (expression.type) {
      case "Identifier": {
        // Follow the closest initialized binding used directly as display text.
        const definition = (bindings.get(expression.name) ?? [])
          .filter((candidate) => candidate.start < expression.start).at(-1);
        if (definition && !resolving.has(definition)) {
          resolving.add(definition);
          visitDisplayExpression(definition.init);
          resolving.delete(definition);
        }
        for (const update of assignments.get(expression.name) ?? []) {
          if (update.start <= (definition?.start ?? -1) || update.start >= expression.start || resolving.has(update)) {
            continue;
          }
          resolving.add(update);
          visitDisplayExpression(update.right);
          resolving.delete(update);
        }
        break;
      }
      case "StringLiteral":
        addLiteral(expression.value, expression.loc.start.line);
        break;
      case "TemplateLiteral":
        expression.quasis.forEach((quasi) => addLiteral(quasi.value.cooked ?? quasi.value.raw, quasi.loc.start.line));
        expression.expressions.forEach(visitDisplayExpression);
        break;
      case "ConditionalExpression":
        visitDisplayExpression(expression.consequent);
        visitDisplayExpression(expression.alternate);
        break;
      case "LogicalExpression":
        visitDisplayExpression(expression.left);
        visitDisplayExpression(expression.right);
        break;
      case "BinaryExpression":
        if (expression.operator === "+") {
          visitDisplayExpression(expression.left);
          visitDisplayExpression(expression.right);
        }
        break;
      case "SequenceExpression":
        visitDisplayExpression(expression.expressions.at(-1));
        break;
      case "ParenthesizedExpression":
      case "TSAsExpression":
      case "TSSatisfiesExpression":
      case "TSNonNullExpression":
      case "TSTypeAssertion":
        visitDisplayExpression(expression.expression);
        break;
      default:
        // Calls such as t("key") are already translated. Their string arguments
        // and conditional tests are not display literals.
        break;
    }
  };
  const visit = (node, parent) => {
    if (!node || typeof node !== "object") return;
    if (node.type === "JSXText") {
      const text = normalized(node.value);
      if (humanText(text)) literals.push({ text, line: node.loc.start.line });
    }
    if (node.type === "JSXAttribute" && humanAttribute.has(node.name?.name)) {
      if (node.value?.type === "StringLiteral") addLiteral(node.value.value, node.loc.start.line);
      else if (node.value?.type === "JSXExpressionContainer") visitDisplayExpression(node.value.expression);
    }
    if (node.type === "ObjectProperty" && humanAttribute.has(node.key?.name ?? node.key?.value)) {
      visitDisplayExpression(node.value);
    }
    if (node.type === "AssignmentExpression" && node.left?.type === "MemberExpression" &&
        !node.left.computed && node.left.property?.name === "title") {
      visitDisplayExpression(node.right);
    }
    if (node.type === "AssignmentPattern" && node.left?.type === "Identifier" &&
        humanDefaultName.test(node.left.name)) {
      visitDisplayExpression(node.right);
    }
    if (node.type === "JSXExpressionContainer" &&
        ["JSXElement", "JSXFragment"].includes(parent?.type)) {
      visitDisplayExpression(node.expression);
    }
    for (const [key, value] of Object.entries(node)) {
      if (["loc", "start", "end", "extra", "tokens", "comments"].includes(key)) continue;
      if (Array.isArray(value)) value.forEach((child) => visit(child, node));
      else if (value && typeof value === "object") visit(value, node);
    }
  };
  visit(ast);
  return literals;
}

function check() {
  const root = resolve(__dirname, "../../..");
  const failures = [];
  for (const file of scopedFiles) {
    const exceptions = allowed[file] ?? [];
    const literals = scanSource(readFileSync(resolve(root, file), "utf8"));
    failures.push(...validateOccurrences(file, literals, exceptions));
  }
  if (failures.length) {
    process.stderr.write(`${failures.join("\n")}\n`);
    process.exitCode = 1;
  } else {
    process.stdout.write(`Migrated UI literals checked in ${scopedFiles.length} scoped files.\n`);
  }
}

function validateOccurrences(file, literals, exceptions) {
  const failures = [];
  const used = new Set();
  for (const { text, line } of literals) {
    const exception = exceptions.find((item) => item.text === text && item.line === line && !used.has(item));
    if (exception) used.add(exception);
    else failures.push(`${file}:${line}: unregistered UI literal ${JSON.stringify(text)}`);
  }
  for (const exception of exceptions) {
    if (!exception.reason || !Number.isInteger(exception.line) || !used.has(exception)) {
      failures.push(`${file}:${exception.line ?? "?"}: stale or unexplained literal exception ${JSON.stringify(exception.text)}`);
    }
  }
  return failures;
}

if (require.main === module) check();
module.exports = { scanSource, validateOccurrences, allowed };
