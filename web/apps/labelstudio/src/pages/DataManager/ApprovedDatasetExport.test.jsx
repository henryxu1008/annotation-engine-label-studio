import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ApprovedDatasetExport } from "./ApprovedDatasetExport";
import { downloadFile } from "../../utils/download";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("../../utils/download", () => ({ downloadFile: jest.fn() }));
const row = { submission_id: 21, revision: 2, result_hash: "a".repeat(64), task_id: 31,
  assignment_id: 41, review: { reviewer: { local_user_id: 51 } } };
const receipt = { release_id: "batch-1", state: "ready", created_at: "2026-10-21T00:00:00Z",
  artifacts: [{ id: "artifact-1", name: "records.json", format: "JSON" }] };
const raw = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });
function mount(projectId = 4) {
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  return { runtime, ...render(<Provider><ApprovedDatasetExport projectId={projectId} /></Provider>), Provider };
}
function defaults(method) {
  if (method === "approvedExportCapabilitiesRaw") return Promise.resolve(raw({ mode: "fork_approved", formats: ["JSON", "JSONL"] }));
  if (method === "approvedExportCandidatesRaw") return Promise.resolve(raw({ results: [row], next_cursor: null }));
  if (method === "approvedExportListRaw") return Promise.resolve(raw({ results: [receipt] }));
  if (method === "approvedExportDetailRaw") return Promise.resolve(raw(receipt));
  if (method === "approvedExportPreviewRaw") return Promise.resolve(raw({ item_count: 1, task_count: 1 }));
  if (method === "approvedExportCreateRaw") return Promise.resolve(raw(receipt, 202));
  throw new Error(`Unexpected ${method}`);
}
async function open() {
  fireEvent.click(await screen.findByTestId("open-approved-batch"));
  await screen.findByTestId("batch-select-21");
}
beforeEach(() => {
  mockCallApi.mockReset(); downloadFile.mockReset();
  Object.defineProperty(window.crypto, "randomUUID", { configurable: true, value: () => "synthetic-idempotency-key" });
});

it("omits an absent cursor and downloads original bytes without rounding snapshot integers", async () => {
  const blob = new Blob(['{"legacy_hash":-7900790079007900790}']);
  mockCallApi.mockImplementation((method) => method === "approvedExportDownloadRaw"
    ? Promise.resolve({ ok: true, blob: async () => blob }) : defaults(method));
  const { unmount, runtime } = mount();
  await open();
  expect(mockCallApi.mock.calls.find(([name]) => name === "approvedExportCandidatesRaw")[1].params).toEqual({ pk: 4 });
  fireEvent.change(screen.getByTestId("batch-history"), { target: { value: receipt.release_id } });
  fireEvent.click(await screen.findByTestId("batch-download-JSON"));
  await waitFor(() => expect(downloadFile).toHaveBeenCalledWith(blob, "records.json"));
  unmount(); runtime.destroy();
});

it("does not download a late body after the project changes", async () => {
  let resolveBlob;
  mockCallApi.mockImplementation((method) => method === "approvedExportDownloadRaw"
    ? Promise.resolve({ ok: true, blob: () => new Promise((resolve) => { resolveBlob = resolve; }) }) : defaults(method));
  const { rerender, unmount, runtime, Provider } = mount();
  await open();
  fireEvent.change(screen.getByTestId("batch-history"), { target: { value: receipt.release_id } });
  fireEvent.click(await screen.findByTestId("batch-download-JSON"));
  await waitFor(() => expect(resolveBlob).toBeDefined());
  rerender(<Provider><ApprovedDatasetExport projectId={5} /></Provider>);
  await act(async () => resolveBlob(new Blob(["late"])));
  expect(downloadFile).not.toHaveBeenCalled();
  unmount(); runtime.destroy();
});

it("does not restore artifact controls from an in-flight poll after permission denial", async () => {
  let resolvePoll;
  let queuedReads = 0;
  const queued = { ...receipt, release_id: "queued", state: "queued", artifacts: [] };
  mockCallApi.mockImplementation((method, options) => {
    if (method === "approvedExportListRaw") return Promise.resolve(raw({ results: [queued, receipt] }));
    if (method === "approvedExportDetailRaw" && options.params.releaseId === "queued") {
      if (++queuedReads === 1) return Promise.resolve(raw(queued));
      return new Promise((resolve) => { resolvePoll = resolve; });
    }
    if (method === "approvedExportDownloadRaw") return Promise.resolve(raw({}, 404));
    return defaults(method);
  });
  const { unmount, runtime } = mount();
  await open();
  fireEvent.change(screen.getByTestId("batch-history"), { target: { value: "queued" } });
  await waitFor(() => expect(resolvePoll).toBeDefined(), { timeout: 3000 });
  fireEvent.change(screen.getByTestId("batch-history"), { target: { value: receipt.release_id } });
  fireEvent.click(await screen.findByTestId("batch-download-JSON"));
  await screen.findByRole("alert");
  await act(async () => resolvePoll(raw({ ...queued, state: "ready", artifacts: receipt.artifacts })));
  expect(screen.queryByTestId("batch-download-JSON")).toBeNull();
  expect(downloadFile).not.toHaveBeenCalled();
  act(() => runtime.updateLocale("zh-CN"));
  expect(screen.getByRole("alert").textContent).toContain("来源已不可用");
  unmount(); runtime.destroy();
});
