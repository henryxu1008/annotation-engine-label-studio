import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ReviewBatches } from "./ReviewBatches";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
jest.mock("./ReviewerWorkspace", () => ({ ReviewerWorkspace: () => null }));

const hash = "a".repeat(64);
const preview = { plan_hash: hash, plan: { algorithm: "classification-conflict-ranked/v1",
  candidates: [{ submission_id: 1, selected: true }, { submission_id: 2, selected: false }],
  priority: { schema: "review-classification-priority/v1", batch_id: 5, manifest_hash: hash,
    evidence: [{ submission_id: 1, state: "conflict" }, { submission_id: 2, state: "conflict" }] } } };
const mount = (locale) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  return { ...render(<Provider><ReviewBatches projectId={4} /></Provider>), runtime };
};
beforeEach(() => {
  mockCallApi.mockReset();
  mockCallApi.mockImplementation((method) => {
    if (method === "reviewBatches") return Promise.resolve({ results: { batches: [] } });
    if (method === "reviewBatchCapability") return Promise.resolve({ enabled: true, can_create: true, can_prioritize: true });
    if (method === "classificationBatches") return Promise.resolve({ results: [{ id: 5, name: "Frozen", manifest_hash: hash }] });
    if (method === "reviewBatchPreview") return Promise.resolve(preview);
    return Promise.resolve({ error: true });
  });
});
const choose = async (zh) => {
  fireEvent.click(await screen.findByLabelText(zh ? "优先抽审冻结分类分歧" : "Prioritize frozen classification conflicts"));
  await screen.findByRole("option", { name: "#5 · Frozen" });
  fireEvent.change(screen.getByLabelText(zh ? "冻结分类批次" : "Frozen classification batch"), { target: { value: "5" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "预览抽样" : "Preview sampling" }));
  await screen.findByTestId("review-batch-preview");
};

it.each(["en-US", "zh-CN"])("shows frozen priority coverage and retains original command after failed reads in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const { unmount, runtime } = mount(locale);
  await choose(zh);
  expect(screen.getByTestId("review-priority-summary").textContent).toContain(zh ? "未覆盖全部分歧" : "does not cover every conflict");
  fireEvent.click(screen.getByRole("button", { name: zh ? "创建／重试" : "Create / retry" }));
  const recovery = await screen.findByRole("button", { name: zh ? "恢复原批次创建" : "Recover original batch creation" });
  await waitFor(() => expect(document.activeElement).toBe(recovery));
  mockCallApi.mockResolvedValue({ error: true });
  fireEvent.click(screen.getByRole("button", { name: zh ? "刷新批次" : "Refresh batch" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: zh ? "预览抽样" : "Preview sampling" })).toBeNull());
  fireEvent.click(recovery);
  await waitFor(() => expect(mockCallApi.mock.calls.filter(([m]) => m === "reviewBatchCreate")).toHaveLength(2));
  const calls = mockCallApi.mock.calls.filter(([m]) => m === "reviewBatchCreate");
  expect(calls[0][1].body).toEqual(calls[1][1].body);
  expect(calls[0][1].headers).toEqual(calls[1][1].headers);
  expect(calls[0][1].body.priority).toEqual({ kind: "classification_conflict", batch_id: 5, manifest_hash: hash });
  expect(mockCallApi.mock.calls.filter(([m]) => m === "reviewBatchPreview")).toHaveLength(1);
  unmount(); runtime.destroy();
});

it("rejects a malformed or mismatched priority preview", async () => {
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => method === "reviewBatchPreview"
    ? Promise.resolve({ ...preview, plan: { ...preview.plan, priority: { ...preview.plan.priority, batch_id: 999 } } }) : original(method, args));
  const { unmount, runtime } = mount("en-US");
  fireEvent.click(await screen.findByLabelText("Prioritize frozen classification conflicts"));
  await screen.findByRole("option", { name: "#5 · Frozen" });
  fireEvent.change(screen.getByLabelText("Frozen classification batch"), { target: { value: "5" } });
  fireEvent.click(screen.getByRole("button", { name: "Preview sampling" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Create / retry" })).toBeNull();
  unmount(); runtime.destroy();
});

it("preserves an in-flight original key across blur and ignores its late acknowledgement", async () => {
  let resolve;
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => method === "reviewBatchCreate"
    ? new Promise((done) => { resolve = done; }) : original(method, args));
  const { unmount, runtime } = mount("en-US");
  await choose(false);
  fireEvent.click(screen.getByRole("button", { name: "Create / retry" }));
  fireEvent(window, new Event("blur"));
  await screen.findByRole("button", { name: "Recover original batch creation" });
  const first = mockCallApi.mock.calls.find(([m]) => m === "reviewBatchCreate")[1];
  resolve({ id: 9, plan_hash: hash, idempotency_key: first.headers["Idempotency-Key"] });
  await waitFor(() => expect(screen.queryByText(/Batch #9 was saved/)).toBeNull());
  expect(screen.getByRole("button", { name: "Recover original batch creation" })).toBeTruthy();
  unmount(); runtime.destroy();
});

it("permits a fresh explicit preview only for a recognized first-attempt rejection with matching HTTP status", async () => {
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => method === "reviewBatchCreate"
    ? Promise.resolve({ error: true, $meta: { status: 409 }, display_context: { reason: "REVIEW_PRIORITY_SOURCE_UNAVAILABLE" } })
    : original(method, args));
  const { unmount, runtime } = mount("en-US");
  await choose(false);
  fireEvent.click(screen.getByRole("button", { name: "Create / retry" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Recover original batch creation" })).toBeNull();
  expect(screen.getByRole("button", { name: "Preview sampling" }).disabled).toBe(false);
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("discards an initial priority detail that arrives after blur in %s", async (locale) => {
  let resolveDetail;
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => {
    if (method === "reviewBatches") return Promise.resolve({ results: { batches: [{ id: 9, name: "Private batch" }] } });
    if (method === "reviewBatchCapability") return Promise.resolve({ enabled: true, can_create: true,
      can_prioritize: true, can_read_priority: true, viewer_hash: hash });
    if (method === "reviewBatchDetail") return new Promise((done) => { resolveDetail = done; });
    return original(method, args);
  });
  const { unmount, runtime } = mount(locale);
  fireEvent.click(await screen.findByRole("button", { name: "Private batch" }));
  fireEvent(window, new Event("blur"));
  await act(async () => resolveDetail({ id: 9, name: "Private batch", role: "manager", plan_hash: hash,
    viewer_hash: hash, algorithm: "classification-conflict-ranked/v1", as_of: "2026-10-25T10:00:00Z",
    rule: { rate: 50, seed: "fixed" }, candidate_count: 0, selected_count: 0, items: [] }));
  expect(screen.queryByTestId("review-batch-progress")).toBeNull();
  expect(screen.queryByRole("heading", { name: "Private batch" })).toBeNull();
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("discards an unconfirmed priority preview that arrives after blur in %s", async (locale) => {
  let resolvePreview;
  const zh = locale === "zh-CN";
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => method === "reviewBatchPreview"
    ? new Promise((done) => { resolvePreview = done; }) : original(method, args));
  const { unmount, runtime } = mount(locale);
  fireEvent.click(await screen.findByLabelText(zh ? "优先抽审冻结分类分歧" : "Prioritize frozen classification conflicts"));
  await screen.findByRole("option", { name: "#5 · Frozen" });
  fireEvent.change(screen.getByLabelText(zh ? "冻结分类批次" : "Frozen classification batch"), { target: { value: "5" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "预览抽样" : "Preview sampling" }));
  fireEvent(window, new Event("blur"));
  await act(async () => resolvePreview(preview));
  expect(screen.queryByTestId("review-batch-preview")).toBeNull();
  expect(screen.queryByRole("button", { name: zh ? "创建／重试" : "Create / retry" })).toBeNull();
  expect(mockCallApi.mock.calls.some(([method]) => method === "reviewBatchCreate")).toBe(false);
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("discards a pending frozen-analysis selector after blur in %s", async (locale) => {
  let resolveList;
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => method === "classificationBatches"
    ? new Promise((done) => { resolveList = done; }) : original(method, args));
  const { unmount, runtime } = mount(locale);
  fireEvent.click(await screen.findByLabelText(locale === "zh-CN" ? "优先抽审冻结分类分歧" : "Prioritize frozen classification conflicts"));
  fireEvent(window, new Event("blur"));
  await act(async () => resolveList({ results: [{ id: 5, name: "Frozen", manifest_hash: hash }] }));
  expect(screen.queryByRole("option", { name: "#5 · Frozen" })).toBeNull();
  expect(screen.queryByTestId("review-priority-selector")).toBeNull();
  unmount(); runtime.destroy();
});
