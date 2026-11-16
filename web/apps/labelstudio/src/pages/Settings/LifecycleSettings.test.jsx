import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { LifecycleSettings } from "./LifecycleSettings";

const mockCallApi = jest.fn();
const noLegacyExports = {
  exports: 0,
  conversions: 0,
  releases: 0,
  release_attempts: 0,
  snapshots: 0,
  audit_packages: 0,
  audit_attempts: 0,
  storage_local_import: 0,
  storage_local_export: 0,
  storage_redis_import: 0,
  storage_redis_export: 0,
  storage_s3_import: 0,
  storage_s3_export: 0,
  storage_gcs_import: 0,
  storage_gcs_export: 0,
  storage_azure_import: 0,
  storage_azure_export: 0,
};
const noLegacyDetails = { items: [], possibly_truncated: false, next_cursor: null };
const noStoragePages = { possibly_truncated: false, operations_next_cursor: null };
let mockProjectId = 4;
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("../../providers/ProjectProvider", () => ({ useProject: () => ({ project: { id: mockProjectId } }) }));
jest.mock("@humansignal/i18n", () => ({ useLocaleTranslation: () => ({ t: (key) => key }) }));
jest.mock("@humansignal/ui", () => ({ Button: ({ children, ...props }) => <button {...props}>{children}</button> }));

beforeEach(() => {
  mockCallApi.mockReset();
  mockProjectId = 4;
});

it("does not apply a late response after switching projects", async () => {
  let completeOld;
  mockCallApi.mockImplementation((method, options) => {
    if (method === "projectLifecycle" && options.params.pk === 4) {
      return new Promise((resolve) => {
        completeOld = resolve;
      });
    }
    if (method === "projectLifecycle") return Promise.resolve({ state: "paused", revision: 9 });
    if (method === "projectStorageOperations") return Promise.resolve({ operations: [] });
    throw new Error(method);
  });
  const view = render(<LifecycleSettings />);
  await waitFor(() => expect(completeOld).toBeDefined());
  mockProjectId = 5;
  view.rerender(<LifecycleSettings />);
  await screen.findByText("lifecycleState_paused");
  await act(async () => completeOld({ state: "active", revision: 1 }));
  expect(screen.getByText("lifecycleState_paused").tagName).toBe("STRONG");
});

it("preserves a failed transition and requires an explicit refresh before retry", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 7 };
    if (method === "projectStorageOperations")
      return {
        operations: [],
        ...noStoragePages,
        legacy_export_blockers: noLegacyExports,
        legacy_export_details: noLegacyDetails,
      };
    if (method === "projectLifecycleTransition") return { error: true, status: 500 };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  await screen.findByText("lifecycleState_active");
  fireEvent.change(screen.getByLabelText("lifecycleTarget"), { target: { value: "paused" } });
  fireEvent.change(screen.getByLabelText("lifecycleReason"), { target: { value: "maintenance" } });
  fireEvent.click(screen.getByText("lifecycleApply"));
  await screen.findByRole("alert");
  expect(mockCallApi).toHaveBeenCalledWith(
    "projectLifecycleTransition",
    expect.objectContaining({
      body: { target: "paused", expected_revision: 7, reason: "maintenance" },
    }),
  );
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
  expect(screen.getByLabelText("lifecycleReason").value).toBe("maintenance");
  expect(mockCallApi.mock.calls.filter(([method]) => method === "projectLifecycle")).toHaveLength(1);
  fireEvent.click(screen.getByText("lifecycleRefresh"));
  await waitFor(() => expect(screen.getByLabelText("lifecycleReason").disabled).toBe(false));
});

it("shows unresolved export records and prevents a state change", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations")
      return {
        operations: [],
        ...noStoragePages,
        legacy_export_blockers: { ...noLegacyExports, releases: 2, release_attempts: 1 },
        legacy_export_details: {
          items: [
            { kind: "releases", id: "receipt-1", state: "failed_retryable", parent_id: null },
            { kind: "release_attempts", id: "attempt-1", state: "expired", parent_id: "receipt-1" },
          ],
          possibly_truncated: true,
          next_cursor: "signed-cursor",
        },
      };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  await screen.findByText("lifecycleLegacyExportsBlocked");
  expect(screen.getByText("lifecycleLegacyExport_releases: 2")).toBeTruthy();
  expect(screen.getByText("lifecycleLegacyExport_releases #receipt-1 · failed_retryable")).toBeTruthy();
  expect(
    screen.getByText("lifecycleLegacyExport_release_attempts #attempt-1 · expired · lifecycleLegacyParent #receipt-1"),
  ).toBeTruthy();
  expect(screen.getByText("lifecycleLegacyDetailsTruncated")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("lifecycleTarget"), { target: { value: "paused" } });
  fireEvent.change(screen.getByLabelText("lifecycleReason"), { target: { value: "maintenance" } });
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
  fireEvent.submit(screen.getByText("lifecycleApply").closest("form"));
  expect(mockCallApi.mock.calls.filter(([method]) => method === "projectLifecycleTransition")).toHaveLength(0);
});

it("does not enable state changes when an older server omits export inventory", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations") return { operations: [] };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  await screen.findByText("lifecycleLegacyExportsUnknown");
  fireEvent.change(screen.getByLabelText("lifecycleTarget"), { target: { value: "paused" } });
  fireEvent.change(screen.getByLabelText("lifecycleReason"), { target: { value: "maintenance" } });
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
});

it("shows legacy cloud sync failures and prevents a state change", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations")
      return {
        operations: [],
        ...noStoragePages,
        legacy_export_blockers: { ...noLegacyExports, storage_s3_export: 1 },
        legacy_export_details: {
          items: [{ kind: "storage_s3_export", id: "7", state: "failed", parent_id: null }],
          possibly_truncated: false,
          next_cursor: null,
        },
      };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  await screen.findByText("lifecycleLegacyExportsBlocked");
  expect(screen.getByText("lifecycleLegacyExport_storage_s3_export #7 · failed")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("lifecycleTarget"), { target: { value: "paused" } });
  fireEvent.change(screen.getByLabelText("lifecycleReason"), { target: { value: "maintenance" } });
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
});

it("blocks transitions if blocker counts and receipt details disagree", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations")
      return {
        operations: [],
        ...noStoragePages,
        legacy_export_blockers: noLegacyExports,
        legacy_export_details: {
          items: [{ kind: "exports", id: "42", state: "running", parent_id: null }],
          possibly_truncated: false,
          next_cursor: null,
        },
      };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  await screen.findByText("lifecycleLegacyExportsBlocked");
  fireEvent.change(screen.getByLabelText("lifecycleTarget"), { target: { value: "paused" } });
  fireEvent.change(screen.getByLabelText("lifecycleReason"), { target: { value: "maintenance" } });
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
});

it("loads the next receipt page without enabling state changes", async () => {
  mockCallApi.mockImplementation(async (method, options) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations") {
      const next = options.params.legacy_cursor === "signed-cursor";
      return {
        operations: [],
        ...noStoragePages,
        legacy_export_blockers: { ...noLegacyExports, exports: 51 },
        legacy_export_details: next
          ? {
              items: [{ kind: "exports", id: "51", state: "failed", parent_id: null }],
              possibly_truncated: false,
              next_cursor: null,
            }
          : {
              items: [{ kind: "exports", id: "1", state: "running", parent_id: null }],
              possibly_truncated: true,
              next_cursor: "signed-cursor",
            },
      };
    }
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  fireEvent.click(await screen.findByText("lifecycleLegacyNext"));
  await screen.findByText("lifecycleLegacyExport_exports #51 · failed");
  expect(mockCallApi).toHaveBeenCalledWith(
    "projectStorageOperations",
    expect.objectContaining({ params: { pk: 4, legacy_cursor: "signed-cursor" } }),
  );
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
  expect(screen.queryByText("lifecycleLegacyExport_exports #1 · running")).toBeNull();
  fireEvent.click(screen.getByText("lifecycleLegacyPrevious"));
  await screen.findByText("lifecycleLegacyExport_exports #1 · running");
  expect(screen.queryByText("lifecycleLegacyExport_exports #51 · failed")).toBeNull();
});

it("ignores a late receipt page after switching projects", async () => {
  let finishPage;
  mockCallApi.mockImplementation((method, options) => {
    if (method === "projectLifecycle") return Promise.resolve({ state: "active", revision: 1 });
    if (method === "projectStorageOperations" && options.params.legacy_cursor) {
      return new Promise((resolve) => {
        finishPage = resolve;
      });
    }
    if (method === "projectStorageOperations" && options.params.pk === 4) {
      return Promise.resolve({
        operations: [],
        ...noStoragePages,
        legacy_export_blockers: { ...noLegacyExports, exports: 51 },
        legacy_export_details: {
          items: [{ kind: "exports", id: "1", state: "running", parent_id: null }],
          possibly_truncated: true,
          next_cursor: "signed-cursor",
        },
      });
    }
    if (method === "projectStorageOperations") {
      return Promise.resolve({
        operations: [],
        ...noStoragePages,
        legacy_export_blockers: noLegacyExports,
        legacy_export_details: noLegacyDetails,
      });
    }
    throw new Error(method);
  });
  const view = render(<LifecycleSettings />);
  fireEvent.click(await screen.findByText("lifecycleLegacyNext"));
  await waitFor(() => expect(finishPage).toBeDefined());
  mockProjectId = 5;
  view.rerender(<LifecycleSettings />);
  await screen.findByText("lifecycleLegacyExportsNone");
  await act(async () =>
    finishPage({
      operations: [],
      ...noStoragePages,
      legacy_export_blockers: { ...noLegacyExports, exports: 1 },
      legacy_export_details: {
        items: [{ kind: "exports", id: "old-project", state: "failed", parent_id: null }],
        possibly_truncated: false,
        next_cursor: null,
      },
    }),
  );
  expect(screen.getByText("lifecycleLegacyExportsNone")).toBeTruthy();
  expect(screen.queryByText("lifecycleLegacyExport_exports #old-project · failed")).toBeNull();
});

it("blocks transitions when counts exist but receipt details are missing", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations")
      return { operations: [], ...noStoragePages, legacy_export_blockers: noLegacyExports };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  await screen.findByText("lifecycleLegacyDetailsUnknown");
  fireEvent.change(screen.getByLabelText("lifecycleTarget"), { target: { value: "paused" } });
  fireEvent.change(screen.getByLabelText("lifecycleReason"), { target: { value: "maintenance" } });
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
});

it("pages storage ledger operations without losing unresolved work", async () => {
  mockCallApi.mockImplementation(async (method, options) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations") {
      const next = options.params.storage_cursor === "storage-cursor";
      return {
        operations: [
          {
            id: next ? "op2" : "op1",
            state: "uncertain",
            operation: "export_sync",
            storage_model: "s3",
            storage_id: 1,
          },
        ],
        possibly_truncated: !next,
        operations_next_cursor: next ? null : "storage-cursor",
        legacy_export_blockers: noLegacyExports,
        legacy_export_details: noLegacyDetails,
      };
    }
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  fireEvent.click(await screen.findByText("lifecycleStorageNext"));
  await waitFor(() => expect(screen.getByRole("radio").value).toBe("op2"));
  expect(mockCallApi).toHaveBeenCalledWith(
    "projectStorageOperations",
    expect.objectContaining({ params: { pk: 4, storage_cursor: "storage-cursor" } }),
  );
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
  fireEvent.click(screen.getByText("lifecycleStoragePrevious"));
  await waitFor(() => expect(screen.getByRole("radio").value).toBe("op1"));
});

it("preserves each inventory cursor when paging the other inventory", async () => {
  mockCallApi.mockImplementation(async (method, options) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations") {
      const legacyNext = options.params.legacy_cursor === "legacy-cursor";
      const storageNext = options.params.storage_cursor === "storage-cursor";
      return {
        operations: [
          {
            id: storageNext ? "op2" : "op1",
            state: "uncertain",
            operation: "export_sync",
            storage_model: "s3",
            storage_id: 1,
          },
        ],
        possibly_truncated: !storageNext,
        operations_next_cursor: storageNext ? null : "storage-cursor",
        legacy_export_blockers: { ...noLegacyExports, exports: 51 },
        legacy_export_details: legacyNext
          ? {
              items: [{ kind: "exports", id: "51", state: "failed", parent_id: null }],
              possibly_truncated: false,
              next_cursor: null,
            }
          : {
              items: [{ kind: "exports", id: "1", state: "running", parent_id: null }],
              possibly_truncated: true,
              next_cursor: "legacy-cursor",
            },
      };
    }
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  fireEvent.click(await screen.findByText("lifecycleLegacyNext"));
  await screen.findByText("lifecycleLegacyExport_exports #51 · failed");
  fireEvent.click(screen.getByText("lifecycleStorageNext"));
  await waitFor(() => expect(screen.getByRole("radio").value).toBe("op2"));
  expect(screen.getByText("lifecycleLegacyExport_exports #51 · failed")).toBeTruthy();
  expect(mockCallApi).toHaveBeenCalledWith(
    "projectStorageOperations",
    expect.objectContaining({
      params: { pk: 4, storage_cursor: "storage-cursor", legacy_cursor: "legacy-cursor" },
    }),
  );
});

it("locks state changes when a pagination request fails", async () => {
  mockCallApi.mockImplementation(async (method, options) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations" && options.params.storage_cursor) return { error: true, status: 400 };
    if (method === "projectStorageOperations")
      return {
        operations: [{ id: "op1", state: "uncertain", operation: "export_sync", storage_model: "s3", storage_id: 1 }],
        possibly_truncated: true,
        operations_next_cursor: "expired",
        legacy_export_blockers: noLegacyExports,
        legacy_export_details: noLegacyDetails,
      };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  fireEvent.click(await screen.findByText("lifecycleStorageNext"));
  await screen.findByRole("alert");
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
});

it("blocks transitions when storage pagination metadata is missing", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations")
      return { operations: [], legacy_export_blockers: noLegacyExports, legacy_export_details: noLegacyDetails };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  await screen.findByText("lifecycleStorageInventoryUnknown");
  fireEvent.change(screen.getByLabelText("lifecycleTarget"), { target: { value: "paused" } });
  fireEvent.change(screen.getByLabelText("lifecycleReason"), { target: { value: "maintenance" } });
  expect(screen.getByText("lifecycleApply").disabled).toBe(true);
});

it("requires independent attestation for an uncertain external outcome", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations")
      return {
        operations: [
          {
            id: "op1",
            state: "uncertain",
            operation: "webhook_task_created",
            storage_model: "webhooks.webhook",
            storage_id: 1,
          },
        ],
      };
    if (method === "projectStorageOperationResolve") return { ok: true };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  fireEvent.click(await screen.findByRole("radio"));
  fireEvent.change(screen.getByLabelText("lifecycleReason", { selector: "#lifecycle-resolution-reason" }), {
    target: { value: "worker stopped" },
  });
  expect(screen.getByText("lifecycleResolve").disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByText("lifecycleResolve"));
  await waitFor(() =>
    expect(mockCallApi).toHaveBeenCalledWith(
      "projectStorageOperationResolve",
      expect.objectContaining({
        params: { pk: 4, operation: "op1" },
        body: { action: "confirm_uncertain", reason: "worker stopped", attested: true },
      }),
    ),
  );
});

it("requests cancellation of a server-identified unsubmitted S3 attempt without an attestation", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "paused", revision: 1 };
    if (method === "projectStorageOperations")
      return {
        operations: [
          {
            id: "op1",
            state: "running",
            operation: "annotation_export",
            storage_model: "io_storages.s3exportstorage",
            storage_id: 1,
            can_cancel_s3_preflight: true,
          },
        ],
        ...noStoragePages,
        legacy_export_blockers: noLegacyExports,
        legacy_export_details: noLegacyDetails,
      };
    if (method === "projectStorageOperationResolve") return { error: true, status: 409 };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  fireEvent.click(await screen.findByRole("radio"));
  expect(screen.getByText("lifecycleResolution_cancel_s3_preflight")).toBeTruthy();
  expect(screen.queryByRole("checkbox")).toBeNull();
  fireEvent.change(screen.getByLabelText("lifecycleReason", { selector: "#lifecycle-resolution-reason" }), {
    target: { value: "cancel unsubmitted attempt" },
  });
  fireEvent.click(screen.getByText("lifecycleResolve"));
  await screen.findByRole("alert");
  expect(mockCallApi).toHaveBeenCalledWith(
    "projectStorageOperationResolve",
    expect.objectContaining({
      params: { pk: 4, operation: "op1" },
      body: { action: "cancel_s3_preflight", reason: "cancel unsubmitted attempt", attested: false },
    }),
  );
  expect(screen.getByText("lifecycleResolve").disabled).toBe(true);
  expect(screen.getByLabelText("lifecycleReason", { selector: "#lifecycle-resolution-reason" }).value).toBe(
    "cancel unsubmitted attempt",
  );
});

it("does not offer attestation-only recovery for uncertain cloud operations", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations")
      return {
        operations: [
          {
            id: "cloud",
            state: "uncertain",
            operation: "export_sync",
            storage_model: "io_storages.s3exportstorage",
            storage_id: 1,
          },
        ],
        ...noStoragePages,
        legacy_export_blockers: noLegacyExports,
        legacy_export_details: noLegacyDetails,
      };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  const radio = await screen.findByRole("radio");
  expect(radio.disabled).toBe(true);
  expect(screen.getByText(/lifecycleCloudRecoveryUnavailable/)).toBeTruthy();
  expect(screen.queryByText("lifecycleResolve")).toBeNull();
});

it("does not offer cancellation for an unknown queued worker", async () => {
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state: "active", revision: 1 };
    if (method === "projectStorageOperations")
      return {
        operations: [
          {
            id: "old",
            state: "queued",
            operation: "legacy_worker",
            storage_model: "plugin.unknownstorage",
            storage_id: 1,
          },
        ],
        ...noStoragePages,
        legacy_export_blockers: noLegacyExports,
        legacy_export_details: noLegacyDetails,
      };
    throw new Error(method);
  });
  render(<LifecycleSettings />);
  expect((await screen.findByRole("radio")).disabled).toBe(true);
  expect(screen.getByText(/lifecycleQueueRecoveryUnavailable/)).toBeTruthy();
});
