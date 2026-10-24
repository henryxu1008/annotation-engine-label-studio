import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ClassificationBatches, ClassificationBatchEntry } from "./ClassificationBatches";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const counts = { agreement: 0, disagreement: 1, unavailable: 0 };
const meta = { id: 7, name: "Synthetic batch", schema: "classification-batches/v1", rule: "classification-exact-sets/v1",
  selection: "active-assignment-latest/v1", created_at: "created", calculated_at: "frozen-time", scope_enumerated_at: "preview-time",
  visible_task_count: 1, visibility: "project", manifest_hash: "frozen-hash" };
const row = { task_id: 8, outcome: "disagreement", reason: null, agreement: 1 / 3, matching_pairs: 1, total_pairs: 3 };
const item = { ...row, status: "comparable", inputs: [1, 2, 3].map((id) => ({ submission_id: id, revision: 1, submitter_id: id + 10,
  result_hash: `source-${id}`, config_hash: "config" })), locations: [{ from_name: "sentiment", to_name: "text",
  matching_pairs: 1, total_pairs: 3, agreement: 1 / 3, disagrees: true,
  values: [{ submission_id: 1, choices: ["Positive"] }, { submission_id: 2, choices: ["Negative"] }] }] };
const respond = async (method, options) => {
  switch (method) {
    case "classificationBatchCapability": return { enabled: true, can_create: true };
    case "classificationBatches": return { count: 1, next: null, previous: null, results: [meta] };
    case "classificationBatchPreview": return { schema: meta.schema, preview_token: "signed", plan_hash: "plan",
      task_ids: [8], display_counts: counts, scope_enumerated_at: "preview-time" };
    case "createClassificationBatch": return { ...meta, replayed: false, idempotency_key: options.headers["Idempotency-Key"] };
    case "classificationBatchDetail": return { ...meta, display_counts: counts };
    case "classificationBatchItems": return { batch_id: 7, count: 1, next: null, previous: null, display_counts: counts, results: [row] };
    case "classificationBatchItem": return { ...meta, observed_at: "now", item };
    default: throw new Error(method);
  }
};
const mount = (locale = "en-US", entry = false) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider;
  return { ...render(<Provider>{entry ? <ClassificationBatchEntry projectId={4} /> : <ClassificationBatches projectId={4} />}</Provider>),
    runtime, Provider };
};
beforeEach(() => mockCallApi.mockReset().mockImplementation(respond));
const create = async (zh = false) => {
  fireEvent.click(await screen.findByRole("button", { name: zh ? "新建比较批次" : "New comparison batch" }));
  fireEvent.change(screen.getByLabelText(zh ? "批次名称" : "Batch name"), { target: { value: "Synthetic batch" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "预览比较" : "Preview comparison" }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "创建冻结批次" : "Create frozen batch" }));
};

it.each(["en-US", "zh-CN"])("creates, filters and returns from a frozen comparison in %s", async (locale) => {
  const { runtime, unmount } = mount(locale), zh = locale === "zh-CN";
  await create(zh);
  fireEvent.click(await screen.findByRole("button", { name: zh ? "查看冻结对照" : "View frozen comparison" }));
  await screen.findByTestId("classification-report");
  expect(screen.getByTestId("classification-agreement").textContent).toContain("33.33%");
  expect(screen.getByTestId("classification-report").textContent).toContain("Negative");
  fireEvent.click(screen.getByRole("button", { name: zh ? "返回队列" : "Back to queue" }));
  await screen.findByLabelText(zh ? "比较状态" : "Comparison state");
  expect(screen.getByRole("combobox").value).toBe("disagreement");
  const calls = mockCallApi.mock.calls.filter(([method]) => method === "createClassificationBatch");
  expect(calls).toHaveLength(1);
  expect(calls[0][1].body).toEqual({ preview_token: "signed", expected_plan_hash: "plan" });
  unmount(); runtime.destroy();
});

it("keeps the original body/key after an unknown acknowledgement and read-only refresh", async () => {
  let attempts = 0;
  mockCallApi.mockImplementation(async (method, options) => {
    if (method === "createClassificationBatch" && ++attempts === 1) throw new Error("lost acknowledgement");
    const data = await respond(method, options);
    return method === "createClassificationBatch" ? { ...data, replayed: true } : data;
  });
  const { runtime, unmount } = mount();
  await create();
  await screen.findByRole("button", { name: "Recover original request" });
  fireEvent.click(screen.getByRole("button", { name: "Back to batches" }));
  await screen.findByRole("button", { name: "Refresh read-only view" });
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByRole("button", { name: "Synthetic batch" });
  expect(mockCallApi.mock.calls.filter(([method]) => method === "createClassificationBatch")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Recover original request" }));
  await screen.findByText(/Original batch #7 was recovered/);
  const calls = mockCallApi.mock.calls.filter(([method]) => method === "createClassificationBatch");
  expect(calls).toHaveLength(2);
  expect(calls[1][1].headers).toEqual(calls[0][1].headers);
  expect(calls[1][1].body).toEqual(calls[0][1].body);
  unmount(); runtime.destroy();
});

it("retains a confirmed save when subsequent reads fail without resubmitting", async () => {
  mockCallApi.mockImplementation((method, options) => method === "classificationBatchItems" ? Promise.reject(new Error("read failed")) : respond(method, options));
  const { runtime, unmount } = mount();
  await create();
  await screen.findByRole("alert");
  expect(screen.getByText(/Batch #7 was saved/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await waitFor(() => expect(mockCallApi.mock.calls.filter(([method]) => method === "classificationBatchItems")).toHaveLength(2));
  expect(mockCallApi.mock.calls.filter(([method]) => method === "createClassificationBatch")).toHaveLength(1);
  unmount(); runtime.destroy();
});

it("discards a late source read after blur and displays no cached answers", async () => {
  let resolve;
  mockCallApi.mockImplementation((method, options) => method === "classificationBatchItem" ? new Promise((done) => { resolve = done; }) : respond(method, options));
  const { runtime, unmount } = mount();
  fireEvent.click(await screen.findByRole("button", { name: "Synthetic batch" }));
  fireEvent.click(await screen.findByRole("button", { name: "View frozen comparison" }));
  fireEvent.blur(window);
  await act(async () => resolve({ ...meta, observed_at: "now", item }));
  expect(screen.queryByTestId("classification-report")).toBeNull();
  expect(screen.queryByText("Negative")).toBeNull();
  unmount(); runtime.destroy();
});

it("ignores an old project's late creation acknowledgement", async () => {
  let resolve;
  mockCallApi.mockImplementation((method, options) => method === "createClassificationBatch"
    ? new Promise((done) => { resolve = () => done({ ...meta, replayed: false, idempotency_key: options.headers["Idempotency-Key"] }); }) : respond(method, options));
  const { runtime, unmount, rerender, Provider } = mount();
  await create();
  rerender(<Provider><ClassificationBatches projectId={5} /></Provider>);
  await act(async () => resolve());
  expect(screen.queryByText(/Batch #7 was saved/)).toBeNull();
  expect(screen.queryByRole("button", { name: "Recover original request" })).toBeNull();
  unmount(); runtime.destroy();
});

it("rejects a malformed capability instead of treating truthy strings as permission", async () => {
  mockCallApi.mockResolvedValue({ enabled: "true", can_create: "true" });
  const { runtime, unmount } = mount("en-US", true);
  await act(async () => {});
  expect(screen.queryByTestId("open-classification-batches")).toBeNull();
  unmount(); runtime.destroy();
});

it("shows source unavailability without stale frozen labels", async () => {
  mockCallApi.mockImplementation((method, options) => method === "classificationBatchItem" ? Promise.resolve({ ...meta, observed_at: "now",
    item: { ...item, status: "unavailable", outcome: "unavailable", reason: "source_unavailable", agreement: null,
      matching_pairs: null, total_pairs: null, inputs: [], locations: [] } }) : respond(method, options));
  const { runtime, unmount } = mount();
  fireEvent.click(await screen.findByRole("button", { name: "Synthetic batch" }));
  fireEvent.click(await screen.findByRole("button", { name: "View frozen comparison" }));
  await screen.findByTestId("classification-unavailable");
  expect(screen.queryByTestId("classification-agreement")).toBeNull();
  expect(screen.queryByText("Negative")).toBeNull();
  unmount(); runtime.destroy();
});
