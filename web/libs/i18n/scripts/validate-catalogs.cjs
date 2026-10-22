const fs = require("node:fs");
const path = require("node:path");

const LOCALES = ["en-US", "zh-CN"];
const NAMESPACES = ["common", "app", "projects", "datamanager", "editor", "collaboration", "errors", "taskPackages"];
const PLURAL_SUFFIX = /_(zero|one|two|few|many|other)$/;
const INTERPOLATION = /{{\s*([a-zA-Z][\w.]*)\s*}}/g;

function placeholders(value) {
  return [...new Set([...value.matchAll(INTERPOLATION)].map((match) => match[1]))].sort();
}

function assertSameList(actual, expected, label, errors) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) errors.push(`${label}: expected [${expected}], got [${actual}]`);
}

function readJSON(file, errors) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); }
  catch (error) { errors.push(`${file}: ${error.message}`); return null; }
}

function validateCatalogs(rootDir = path.resolve(__dirname, "../src/catalogs")) {
  const errors = [];
  const catalogs = {};
  for (const locale of LOCALES) {
    catalogs[locale] = {};
    for (const namespace of NAMESPACES) {
      const file = path.join(rootDir, locale, `${namespace}.json`);
      const catalog = readJSON(file, errors);
      if (!catalog || typeof catalog !== "object" || Array.isArray(catalog)) {
        errors.push(`${file}: namespace must be a non-empty JSON object`);
        continue;
      }
      const keys = Object.keys(catalog);
      if (!keys.length) errors.push(`${file}: empty namespace is not translated`);
      for (const key of keys) {
        if (typeof catalog[key] !== "string" || !catalog[key].trim()) errors.push(`${file}:${key}: expected non-empty text`);
        if (!/^[a-zA-Z][\w.-]*$/.test(key)) errors.push(`${file}:${key}: invalid key`);
      }
      catalogs[locale][namespace] = catalog;
    }
  }

  const semanticGroups = {};
  for (const namespace of NAMESPACES) {
    const english = catalogs["en-US"][namespace];
    const chinese = catalogs["zh-CN"][namespace];
    if (!english || !chinese) continue;
    const groups = {};
    for (const locale of LOCALES) {
      groups[locale] = new Map();
      for (const [key, value] of Object.entries(catalogs[locale][namespace])) {
        const suffix = key.match(PLURAL_SUFFIX)?.[1];
        const base = suffix ? key.slice(0, -(suffix.length + 1)) : key;
        const group = groups[locale].get(base) || { plain: null, plural: {} };
        if (suffix) group.plural[suffix] = typeof value === "string" ? value : "";
        else group.plain = typeof value === "string" ? value : "";
        groups[locale].set(base, group);
      }
    }
    semanticGroups[namespace] = groups;
    assertSameList([...groups["zh-CN"].keys()].sort(), [...groups["en-US"].keys()].sort(), `${namespace} semantic keys`, errors);
    for (const [base, enGroup] of groups["en-US"]) {
      const zhGroup = groups["zh-CN"].get(base);
      if (!zhGroup) continue;
      const enPlural = Object.keys(enGroup.plural).length > 0;
      const zhPlural = Object.keys(zhGroup.plural).length > 0;
      if (enPlural !== zhPlural || Boolean(enGroup.plain) !== Boolean(zhGroup.plain)) {
        errors.push(`${namespace}:${base}: plural/plain shape differs`);
        continue;
      }
      if (enGroup.plain) {
        assertSameList(placeholders(zhGroup.plain || ""), placeholders(enGroup.plain || ""), `${namespace}:${base} placeholders`, errors);
      }
      if (!enPlural) continue;
      for (const locale of LOCALES) {
        const group = groups[locale].get(base);
        const allowed = new Set([...new Intl.PluralRules(locale).resolvedOptions().pluralCategories, "zero"]);
        const suffixes = Object.keys(group.plural);
        for (const suffix of suffixes) if (!allowed.has(suffix)) errors.push(`${locale}/${namespace}:${base}_${suffix}: invalid plural category`);
        for (const required of new Intl.PluralRules(locale).resolvedOptions().pluralCategories) {
          if (!(required in group.plural)) errors.push(`${locale}/${namespace}:${base}_${required}: missing plural category`);
        }
      }
      const expected = placeholders(enGroup.plural.other || "");
      for (const locale of LOCALES) {
        for (const [suffix, value] of Object.entries(groups[locale].get(base).plural)) {
          assertSameList(placeholders(value), expected, `${locale}/${namespace}:${base}_${suffix} placeholders`, errors);
        }
      }
    }
  }

  const dynamic = readJSON(path.join(rootDir, "dynamic-keys.json"), errors);
  if (!Array.isArray(dynamic)) errors.push("dynamic-keys.json: expected an array of explicitly allowed keys");
  else {
    for (const key of dynamic) {
      if (typeof key !== "string" || !/^[a-zA-Z][\w.-]*:[a-zA-Z][\w.-]*$/.test(key)) {
        errors.push(`Invalid dynamic key: ${key}`);
        continue;
      }
      const [namespace, name] = key.split(":");
      if (!NAMESPACES.includes(namespace) || !LOCALES.every((locale) => semanticGroups[namespace]?.[locale]?.has(name))) {
        errors.push(`Untranslated dynamic key: ${key}`);
      }
    }
    if (new Set(dynamic).size !== dynamic.length) errors.push("dynamic-keys.json: duplicate key");
  }
  return errors;
}

if (require.main === module) {
  const errors = validateCatalogs();
  if (errors.length) { console.error(errors.join("\n")); process.exitCode = 1; }
  else console.log(`i18n catalogs valid (en-US, zh-CN; ${NAMESPACES.length} namespaces)`);
}

module.exports = { validateCatalogs };
