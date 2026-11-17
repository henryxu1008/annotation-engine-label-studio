import { apiSucceeded, lifecycleTargets, resolutionFor } from "./lifecycleState";

describe("lifecycle policy presentation", () => {
  it("shows only transitions supported by the server", () => {
    expect(lifecycleTargets.active).toEqual(["paused", "completed"]);
    expect(lifecycleTargets.archived).toEqual(["active"]);
    expect(lifecycleTargets.unknown).toBeUndefined();
  });
  it("never offers reconciliation for running operations", () => {
    expect(resolutionFor({ state: "running", operation: "import_sync" })).toBeNull();
    expect(
      resolutionFor({
        state: "queued",
        operation: "import_sync",
        storage_model: "io_storages.localfilesimportstorage",
      }),
    ).toBe("cancel_queued");
    expect(
      resolutionFor({
        state: "queued",
        operation: "annotation_delete_pending",
        storage_model: "io_storages.localfilesexportstorage",
      }),
    ).toBe("confirm_deletion");
    expect(
      resolutionFor({
        state: "uncertain",
        operation: "import_sync",
        storage_model: "io_storages.localfilesimportstorage",
      }),
    ).toBe("confirm_uncertain");
    expect(
      resolutionFor({ state: "uncertain", operation: "import_sync", storage_model: "plugin.unknownstorage" }),
    ).toBeNull();
    expect(
      resolutionFor({
        state: "uncertain",
        operation: "unknown_worker",
        storage_model: "io_storages.localfilesexportstorage",
      }),
    ).toBeNull();
    expect(resolutionFor({ state: "uncertain", operation: "import_sync" })).toBeNull();
    expect(
      resolutionFor({ state: "uncertain", operation: "import_sync", storage_model: "io_storages.s3importstorage" }),
    ).toBeNull();
    expect(
      resolutionFor({
        state: "queued",
        operation: "annotation_delete_pending",
        storage_model: "io_storages.s3exportstorage",
      }),
    ).toBeNull();
    expect(
      resolutionFor({ state: "queued", operation: "import_sync", storage_model: "io_storages.s3importstorage" }),
    ).toBe("cancel_queued");
    expect(
      resolutionFor({
        state: "queued",
        operation: "annotation_export_v3_queued",
        storage_model: "io_storages.s3exportstorage",
      }),
    ).toBe("cancel_queued");
    expect(
      resolutionFor({
        state: "uncertain",
        operation: "annotation_export_v3_queued",
        storage_model: "io_storages.s3exportstorage",
      }),
    ).toBeNull();
    expect(
      resolutionFor({
        state: "uncertain",
        operation: "annotation_export_v3_queued",
        storage_model: "io_storages.localfilesexportstorage",
      }),
    ).toBe("confirm_uncertain");
    expect(
      resolutionFor({
        state: "running",
        operation: "annotation_export",
        storage_model: "io_storages.s3exportstorage",
        can_cancel_s3_preflight: true,
      }),
    ).toBe("cancel_s3_preflight");
    expect(
      resolutionFor({
        state: "uncertain",
        operation: "annotation_export",
        storage_model: "io_storages.s3exportstorage",
        can_cancel_s3_preflight: true,
      }),
    ).toBe("cancel_s3_preflight");
    expect(
      resolutionFor({
        state: "running",
        operation: "annotation_export",
        storage_model: "io_storages.s3exportstorage",
        can_cancel_s3_preflight: "true",
      }),
    ).toBeNull();
    expect(
      resolutionFor({
        state: "running",
        operation: "annotation_export",
        storage_model: "plugin.unknownstorage",
        can_cancel_s3_preflight: true,
      }),
    ).toBeNull();
    expect(
      resolutionFor({
        state: "uncertain",
        operation: "annotation_export",
        storage_model: "io_storages.s3exportstorage",
        can_cancel_s3_preflight: false,
      }),
    ).toBeNull();
    expect(resolutionFor({ state: "queued", operation: "unknown_old_worker" })).toBeNull();
    expect(
      resolutionFor({ state: "queued", operation: "import_sync", storage_model: "plugin.unknownstorage" }),
    ).toBeNull();
  });
  it("does not treat absent or unsuccessful responses as success", () => {
    expect(apiSucceeded(null)).toBe(false);
    expect(apiSucceeded({ error: true })).toBe(false);
    expect(apiSucceeded({ $meta: { ok: false } })).toBe(false);
    expect(apiSucceeded({ $meta: { ok: true } })).toBe(true);
  });
  it("only offers webhook recovery for the server-confirmed pre-send state", () => {
    const entry = {
      state: "running",
      operation: "webhook_task_created",
      storage_model: "webhooks.webhook",
      can_recover_webhook_pre_send: true,
    };
    expect(resolutionFor(entry)).toBe("recover_webhook_pre_send");
    expect(resolutionFor({ ...entry, can_recover_webhook_pre_send: "true" })).toBeNull();
    expect(resolutionFor({ ...entry, operation: "unknown" })).toBeNull();
    expect(resolutionFor({ ...entry, storage_model: "plugin.unknown" })).toBeNull();
    expect(resolutionFor({ ...entry, state: "queued" })).toBe("cancel_queued");
  });
  it("only offers storage recovery for known, server-confirmed pre-effect work", () => {
    const entry = {
      state: "running",
      operation: "import_sync",
      storage_model: "io_storages.localfilesimportstorage",
      can_recover_storage_pre_effect: true,
    };
    expect(resolutionFor(entry)).toBe("recover_storage_pre_effect");
    expect(resolutionFor({ ...entry, can_recover_storage_pre_effect: "true" })).toBeNull();
    expect(resolutionFor({ ...entry, storage_model: "plugin.unknown" })).toBeNull();
    expect(resolutionFor({ ...entry, operation: "unknown" })).toBeNull();
  });
});
