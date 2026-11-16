export const lifecycleTargets = Object.freeze({
  legacy: ["draft", "active"],
  draft: ["active"],
  active: ["paused", "completed"],
  paused: ["active", "completed"],
  completed: ["active", "archived"],
  archived: ["active"],
});

const manualReconciliationModels = new Set([
  "io_storages.localfilesimportstorage",
  "io_storages.localfilesexportstorage",
  "webhooks.webhook",
]);
const versionedImportModels = new Set([
  "io_storages.localfilesimportstorage",
  "io_storages.redisimportstorage",
  "io_storages.s3importstorage",
  "io_storages.gcsimportstorage",
  "io_storages.azureblobimportstorage",
]);
const versionedExportModels = new Set([
  "io_storages.localfilesexportstorage",
  "io_storages.redisexportstorage",
  "io_storages.s3exportstorage",
  "io_storages.gcsexportstorage",
  "io_storages.azureblobexportstorage",
]);
const cancellableWebhookOperations = new Set([
  "webhook_task_created",
  "webhook_project_created",
  "webhook_project_updated",
  "webhook_annotation_created",
  "webhook_annotation_updated",
  "webhook_annotations_deleted",
  "webhook_tasks_deleted",
  "webhook_label_link_created",
  "webhook_label_link_updated",
  "webhook_label_link_deleted",
]);

const supportsManualReconciliation = (entry) =>
  manualReconciliationModels.has(entry.storage_model) &&
  ((entry.storage_model === "io_storages.localfilesimportstorage" && entry.operation === "import_sync") ||
    (entry.storage_model === "io_storages.localfilesexportstorage" &&
      [
        "export_sync",
        "export_sync_new",
        "annotation_export",
        "annotation_export_queued",
        "annotation_export_v3_queued",
        "annotation_delete_pending",
      ].includes(entry.operation)) ||
    (entry.storage_model === "webhooks.webhook" && cancellableWebhookOperations.has(entry.operation)));

const canCancelS3Preflight = (entry) =>
  entry.can_cancel_s3_preflight === true &&
  entry.storage_model === "io_storages.s3exportstorage" &&
  entry.operation === "annotation_export" &&
  ["running", "uncertain"].includes(entry.state);

export const requiresVerifiedRecovery = (entry) =>
  (entry.state === "uncertain" && !canCancelS3Preflight(entry) && !supportsManualReconciliation(entry)) ||
  (entry.state === "queued" &&
    entry.operation === "annotation_delete_pending" &&
    entry.storage_model !== "io_storages.localfilesexportstorage");

export const resolutionFor = (entry) => {
  if (canCancelS3Preflight(entry)) return "cancel_s3_preflight";
  if (requiresVerifiedRecovery(entry)) return null;
  if (entry.state === "uncertain") return "confirm_uncertain";
  if (entry.state !== "queued") return null;
  if (entry.operation === "annotation_delete_pending") return "confirm_deletion";
  if (entry.operation === "import_sync" && versionedImportModels.has(entry.storage_model)) return "cancel_queued";
  if (
    ["export_sync", "export_sync_new", "annotation_export_queued", "annotation_export_v3_queued"].includes(
      entry.operation,
    ) &&
    versionedExportModels.has(entry.storage_model)
  )
    return "cancel_queued";
  if (cancellableWebhookOperations.has(entry.operation) && entry.storage_model === "webhooks.webhook")
    return "cancel_queued";
  return null;
};

export const apiSucceeded = (result) =>
  Boolean(result && !result.error && result.$meta?.ok !== false && !(result.status >= 400));
