// These path segments are product-owned route metadata, not project names.
export const settingsSectionTitleKeys = Object.freeze({
  "/": "general",
  "/labeling": "labelingInterface",
  "/annotation": "annotation",
  "/members": "members",
  "/lifecycle": "lifecycleTitle",
  "/ml": "model",
  "/predictions": "predictions",
  "/storage": "cloudStorage",
  "/webhooks": "webhooks",
  "/danger-zone": "dangerZone",
});

export const settingsSectionTitleKeyForRoute = (path) => {
  const sectionPath = path.match(/\/settings(\/.*)$/)?.[1];
  return settingsSectionTitleKeys[sectionPath] ?? null;
};
