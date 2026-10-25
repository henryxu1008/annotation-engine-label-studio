import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ReviewBatchEntry, ReviewBatches } from "./ReviewBatches";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
jest.mock("./ReviewerWorkspace", () => ({ ReviewerWorkspace: ({ batchId }) => <div data-testid="batch-queue">{batchId}</div> }));
const detail = {
  id: 7, name: "Sample 样本", role: "reviewer", plan_hash: "a".repeat(64), candidate_count: 2, selected_count: 1,
  algorithm: "sha256-ranked-v1", rule: { rate: 50, seed: "fixed" },
  as_of: "2026-10-23T10:00:00Z",
  items: [
    { submission_id: 1, revision: 1, selected: true, status: "rejected", review_reason: "Fix 修正",
      can_review: true, successor: { id: 3, revision: 2 } },
    { submission_id: 2, revision: 1, selected: false, status: "pending", can_review: false },
  ],
};
const mount = (locale = "en-US", entry = false) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(<Provider>{entry ? <ReviewBatchEntry projectId={4} /> : <ReviewBatches projectId={4} />}</Provider>);
  return { ...view, runtime };
};
beforeEach(() => {
  mockCallApi.mockReset();
  mockCallApi.mockImplementation((method) => {
    if (method === "reviewBatches") return Promise.resolve({ results: { batches: [{ id: 7, name: detail.name }] } });
    if (method === "reviewBatchCapability") return Promise.resolve({ enabled: true, can_create: true });
    if (method === "reviewBatchDetail") return Promise.resolve(detail);
    return Promise.resolve({});
  });
});

it.each(["en-US", "zh-CN"])("shows sample/rework semantics and scoped review in %s", async (locale) => {
  const { unmount, runtime } = mount(locale);
  fireEvent.click(await screen.findByRole("button", { name: detail.name }));
  await screen.findByRole("heading", { name: detail.name });
  expect(screen.getByText(locale === "en-US" ? "Unselected — not approval" : "未抽中，不代表批准")).toBeTruthy();
  expect(screen.getByText(/#3/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: locale === "en-US" ? "Review this sample" : "审核本批样本" }));
  expect(screen.getByTestId("batch-queue").textContent).toBe("7");
  unmount(); runtime.destroy();
});

it("retains the same request and key after a transport failure", async () => {
  mockCallApi.mockImplementation((method) => {
    if (method === "reviewBatches") return Promise.resolve({ results: { batches: [] } });
    if (method === "reviewBatchCapability") return Promise.resolve({ enabled: true, can_create: true });
    if (method === "reviewBatchPreview") return Promise.resolve({ plan_hash: "a".repeat(64),
      plan: { candidates: [{ selected: true }] } });
    if (method === "reviewBatchCreate") return Promise.resolve({ error: true });
    return Promise.resolve({});
  });
  const { unmount, runtime } = mount();
  fireEvent.change(await screen.findByLabelText("Batch name"), { target: { value: "Trial" } });
  fireEvent.click(screen.getByRole("button", { name: "Preview sampling" }));
  fireEvent.click(await screen.findByRole("button", { name: "Create / retry" }));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Create / retry" }));
  await waitFor(() => expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchCreate")).toHaveLength(2));
  const calls = mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchCreate");
  expect(calls[0][1].body).toEqual(calls[1][1].body);
  expect(calls[0][1].headers).toEqual(calls[1][1].headers);
  unmount(); runtime.destroy();
});

it("clears stale rows and creation controls on revoked access", async () => {
  const { unmount, runtime } = mount();
  fireEvent.click(await screen.findByRole("button", { name: detail.name }));
  await screen.findByRole("heading", { name: detail.name });
  mockCallApi.mockResolvedValue({ error: true });
  fireEvent.click(screen.getByRole("button", { name: "Refresh batch" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("heading", { name: detail.name })).toBeNull();
  expect(screen.queryByLabelText("Batch name")).toBeNull();
  unmount(); runtime.destroy();
});

it("hides the entry when the feature is unavailable", async () => {
  mockCallApi.mockResolvedValue({ error: true });
  const { unmount, runtime } = mount("en-US", true);
  await waitFor(() => expect(mockCallApi).toHaveBeenCalled());
  expect(screen.queryByTestId("open-review-batches")).toBeNull();
  unmount(); runtime.destroy();
});

it("returns to the same batch filter and restores focus after the authority refresh", async () => {
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => method === "reviewBatchRework"
    ? Promise.resolve({ results: { source: { id: 1 }, revisions: [] } }) : original(method, args));
  const { unmount, runtime } = mount();
  fireEvent.click(await screen.findByRole("button", { name: detail.name }));
  fireEvent.click(await screen.findByRole("button", { name: "Rework follow-up" }));
  fireEvent.click(screen.getByRole("button", { name: "Compare rework revisions" }));
  fireEvent.click(await screen.findByRole("button", { name: "Back to batch" }));
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Compare rework revisions" })));
  expect(screen.getByRole("button", { name: "Rework follow-up" }).getAttribute("aria-pressed")).toBe("true");
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("keeps refresh reachable after a priority comparison loses focus in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const original = mockCallApi.getMockImplementation();
  const viewerHash = "b".repeat(64);
  mockCallApi.mockImplementation((method, args) => {
    if (method === "reviewBatchCapability") return Promise.resolve({ enabled: true, can_create: true,
      can_prioritize: true, can_read_priority: true, viewer_hash: viewerHash });
    if (method === "reviewBatchDetail") return Promise.resolve({ ...detail,
      algorithm: "classification-conflict-ranked/v1", viewer_hash: viewerHash });
    if (method === "reviewBatchRework") return Promise.resolve({ viewer_hash: viewerHash,
      results: { source: { id: 1 }, revisions: [] } });
    return original(method, args);
  });
  const { unmount, runtime } = mount(locale);
  fireEvent.click(await screen.findByRole("button", { name: detail.name }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "对照返工修订" : "Compare rework revisions" }));
  await screen.findByTestId("rework-comparison");
  fireEvent(window, new Event("blur"));
  const refresh = screen.getByRole("button", { name: zh ? "刷新批次" : "Refresh batch" });
  expect(screen.queryByTestId("rework-comparison")).toBeNull();
  fireEvent.click(refresh);
  await screen.findByRole("heading", { name: detail.name });
  expect(mockCallApi.mock.calls.some(([method]) => method === "reviewSubmission")).toBe(false);
  unmount(); runtime.destroy();
});

it.each([
  ["en-US", true], ["zh-CN", true], ["en-US", false], ["zh-CN", false],
])("retains a dispatched legacy key across blur in %s, pending=%s", async (locale, pending) => {
  const zh = locale === "zh-CN";
  let settle;
  let attempts = 0;
  mockCallApi.mockImplementation((method) => {
    if (method === "reviewBatches") return Promise.resolve({ results: { batches: [] } });
    if (method === "reviewBatchCapability") return Promise.resolve({ enabled: true, can_create: true });
    if (method === "reviewBatchPreview") return Promise.resolve({ plan_hash: "a".repeat(64), plan: { candidates: [{ selected: true }] } });
    if (method === "reviewBatchCreate") {
      attempts += 1;
      return pending && attempts === 1 ? new Promise((resolve) => { settle = resolve; }) : Promise.resolve({ error: true });
    }
    return Promise.resolve({});
  });
  const { unmount, runtime } = mount(locale);
  fireEvent.change(await screen.findByLabelText(zh ? "审核批次名称" : "Batch name"), { target: { value: "Legacy" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "预览抽样" : "Preview sampling" }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "创建／重试" : "Create / retry" }));
  if (!pending) await screen.findByRole("alert");
  fireEvent(window, new Event("blur"));
  const recover = await screen.findByRole("button", { name: zh ? "恢复原批次创建" : "Recover original batch creation" });
  if (pending) await act(async () => settle({ error: true }));
  fireEvent.click(recover);
  await waitFor(() => expect(attempts).toBe(2));
  const calls = mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchCreate");
  for (const key of ["params", "body", "headers"]) expect(calls[0][1][key]).toEqual(calls[1][1][key]);
  expect(calls[0][1].body.priority).toBeUndefined();
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchPreview")).toHaveLength(1);
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("does not turn a definitive legacy rejection into an unknown write after blur in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => {
    if (method === "reviewBatchPreview") return Promise.resolve({ plan_hash: "a".repeat(64), plan: { candidates: [{ selected: true }] } });
    if (method === "reviewBatchCreate") return Promise.resolve({ error: true, $meta: { status: 400 } });
    return original(method, args);
  });
  const { unmount, runtime } = mount(locale);
  fireEvent.change(await screen.findByLabelText(zh ? "审核批次名称" : "Batch name"), { target: { value: "Legacy" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "预览抽样" : "Preview sampling" }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "创建／重试" : "Create / retry" }));
  await screen.findByRole("alert");
  fireEvent(window, new Event("blur"));
  expect(screen.queryByRole("button", { name: zh ? "恢复原批次创建" : "Recover original batch creation" })).toBeNull();
  fireEvent.click(screen.getByTestId("review-batch-refresh"));
  await waitFor(() => expect(screen.getByRole("button", { name: zh ? "预览抽样" : "Preview sampling" }).disabled).toBe(false));
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchCreate")).toHaveLength(1);
  unmount(); runtime.destroy();
});
