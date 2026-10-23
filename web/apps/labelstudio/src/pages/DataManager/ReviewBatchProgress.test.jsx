import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ReviewBatches } from "./ReviewBatches";
import { ReviewerWorkspace } from "./ReviewerWorkspace";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return {
    Button: ({ children, waiting, ...props }) => React.createElement("button", props, children),
    Typography: ({ children }) => React.createElement("span", null, children),
  };
});
jest.mock("../../components/Spinner/Spinner", () => ({ Spinner: () => <span>Loading</span> }));

let batch;
let sources;
const source = (id) => ({ id, revision: 1, status: "pending", submitted_by: { email: "synthetic@example.test" }, result_hash: `hash-${id}`, result_snapshot: { result: [] } });
beforeEach(() => {
  sources = [source(11), source(12)];
  batch = {
    id: 7, name: "Batch seven", role: "reviewer", plan_hash: "a".repeat(64),
    candidate_count: 3, selected_count: 2, as_of: "2026-10-23T10:00:00Z",
    algorithm: "sha256-ranked-v1", rule: { rate: 50, seed: "fixed" },
    items: [11, 12, 13].map((id) => ({ submission_id: id, revision: 1, selected: id !== 13, status: "pending", can_review: id !== 13 })),
  };
  mockCallApi.mockReset();
  mockCallApi.mockImplementation(async (method, args) => {
    if (method === "reviewBatches") return { results: { batches: [{ id: 7, name: batch.name }, { id: 8, name: "Batch eight" }] } };
    if (method === "reviewBatchCapability") return { enabled: true, can_create: false };
    if (method === "reviewBatchDetail") return { ...batch, id: args.params.batch, name: args.params.batch === 8 ? "Batch eight" : "Batch seven" };
    if (method === "reviewBatchQueue") return { results: sources };
    if (method === "reviewSubmission") {
      const id = args.params.submissionPk;
      batch = { ...batch, items: batch.items.map((item) => item.submission_id === id ? { ...item, status: args.body.decision, can_review: false } : item) };
      sources = sources.filter((row) => row.id !== id);
      return { id, status: args.body.decision };
    }
    return {};
  });
});

const mount = async (locale = "en-US") => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(<Provider><ReviewBatches projectId={4} /></Provider>);
  fireEvent.click(await screen.findByRole("button", { name: "Batch seven" }));
  await screen.findByRole("heading", { name: "Batch seven" });
  return { ...view, runtime };
};
const count = (label) => within(screen.getByTestId("review-batch-progress")).getByText(label).parentElement.querySelector("dd").textContent;
const openQueue = async (zh = false) => {
  fireEvent.click(screen.getByRole("button", { name: zh ? "审核本批样本" : "Review this sample" }));
  await screen.findByTestId("review-approve");
};

it("recovers a thrown review transport failure by reading without replaying the command", async () => {
  const { unmount, runtime } = await mount();
  await openQueue();
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => method === "reviewSubmission"
    ? Promise.reject(new Error("response lost")) : original(method, args));
  fireEvent.click(screen.getByTestId("review-approve"));
  await waitFor(() => expect(screen.getByTestId("review-approve").disabled).toBe(false));
  expect(screen.getByTestId("review-error")).toBeTruthy();
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewSubmission")).toHaveLength(1);
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchQueue")).toHaveLength(2);
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("refreshes counts after a committed decision loses its response without claiming confirmation in %s", async (locale) => {
  const { unmount, runtime } = await mount(locale);
  await openQueue(locale === "zh-CN");
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation(async (method, args) => {
    const result = await original(method, args);
    if (method === "reviewSubmission") throw new Error("response lost after commit");
    return result;
  });
  fireEvent.click(screen.getByTestId("review-approve"));
  await waitFor(() => expect(count(locale === "zh-CN" ? "抽中 · 已批准" : "Selected · approved")).toBe("1"));
  expect(await screen.findByTestId("review-error")).toHaveProperty("textContent", locale === "zh-CN"
    ? "审核响应丢失，请核对刷新后的状态再做决定。"
    : "The review response was lost. Check the refreshed state before making another decision.");
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByTestId("review-submission-11")).toBeNull();
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewSubmission")).toHaveLength(1);
  unmount(); runtime.destroy();
});

it.each(["project", "batch", "package"])("discards an in-flight command and old state when the %s scope changes", async (scope) => {
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  const original = mockCallApi.getMockImplementation();
  let resolveOld;
  mockCallApi.mockImplementation((method, args) => method === "reviewSubmission"
    ? new Promise((resolve) => { resolveOld = resolve; }) : original(method, args));
  const onReviewSettled = jest.fn();
  const props = { projectId: 4, batchId: 7, onReviewSettled };
  const { rerender, unmount } = render(<Provider><ReviewerWorkspace {...props} /></Provider>);
  fireEvent.click(await screen.findByTestId("review-approve"));
  sources = [source(21)];
  const changed = scope === "project" ? { projectId: 5 } : scope === "batch" ? { batchId: 8 } : { packageId: 9 };
  rerender(<Provider><ReviewerWorkspace {...props} {...changed} /></Provider>);
  await screen.findByTestId("review-submission-21");
  expect(screen.queryByTestId("review-submission-11")).toBeNull();
  expect(screen.getByTestId("review-approve").disabled).toBe(false);
  await act(async () => resolveOld({ id: 11, status: "approved" }));
  expect(onReviewSettled).not.toHaveBeenCalled();
  expect(screen.getByTestId("review-approve").disabled).toBe(false);
  expect(screen.queryByRole("status")).toBeNull();
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("updates progress after a real scoped decision without remounting the queue in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const { unmount, runtime } = await mount(locale);
  expect(count(zh ? "抽中 · 待审核" : "Selected · pending")).toBe("2");
  expect(screen.getByText(zh ? /数量与抽中率仅覆盖当前可见/ : /Counts and sample rate cover only/)).toBeTruthy();
  await openQueue(zh);
  const queue = screen.getByTestId("reviewer-workspace");
  fireEvent.click(screen.getByTestId("review-approve"));
  await waitFor(() => expect(count(zh ? "抽中 · 已批准" : "Selected · approved")).toBe("1"));
  await waitFor(() => expect(screen.getByTestId("review-approve").disabled).toBe(false));
  expect(count(zh ? "抽中 · 待审核" : "Selected · pending")).toBe("1");
  expect(screen.getByTestId("reviewer-workspace")).toBe(queue);
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewSubmission")).toHaveLength(1);
  expect(mockCallApi.mock.calls.find(([method]) => method === "reviewSubmission")[1].params).toEqual({ submissionPk: 11, review_batch: 7 });
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("retains the saved outcome and retries only the failed detail read in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const { unmount, runtime } = await mount(locale);
  await openQueue(zh);
  const original = mockCallApi.getMockImplementation();
  let failRead = true;
  mockCallApi.mockImplementation((method, args) => method === "reviewBatchDetail" && failRead ? Promise.reject(new Error("offline")) : original(method, args));
  fireEvent.click(screen.getByTestId("review-approve"));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", zh ? "审核已保存，但进度未更新；请刷新批次，不要重复审核。" : "Review saved, but progress could not be updated. Refresh this batch; do not repeat the review.");
  expect(screen.queryByTestId("review-batch-progress")).toBeNull();
  expect(screen.queryByTestId("reviewer-workspace")).toBeNull();
  failRead = false;
  fireEvent.click(screen.getByRole("button", { name: zh ? "刷新批次" : "Refresh batch" }));
  await screen.findByTestId("review-batch-progress");
  expect(count(zh ? "抽中 · 已批准" : "Selected · approved")).toBe("1");
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewSubmission")).toHaveLength(1);
  unmount(); runtime.destroy();
});

it("does not let a saved response from the previous batch replace the active batch", async () => {
  const { unmount, runtime } = await mount();
  await openQueue();
  const original = mockCallApi.getMockImplementation();
  let completeReview;
  mockCallApi.mockImplementation((method, args) => method === "reviewSubmission" ? new Promise((resolve) => { completeReview = resolve; }) : original(method, args));
  fireEvent.click(screen.getByTestId("review-approve"));
  fireEvent.click(screen.getByRole("button", { name: "Batch eight" }));
  await screen.findByRole("heading", { name: "Batch eight" });
  mockCallApi.mockClear();
  await act(async () => completeReview({ id: 11, status: "approved" }));
  expect(screen.getByRole("heading", { name: "Batch eight" })).toBeTruthy();
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchDetail")).toHaveLength(0);
  unmount(); runtime.destroy();
});

it("refreshes the batch even when the post-decision queue transport fails", async () => {
  const { unmount, runtime } = await mount();
  await openQueue();
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation((method, args) => method === "reviewBatchQueue" ? Promise.reject(new Error("offline")) : original(method, args));
  fireEvent.click(screen.getByTestId("review-approve"));
  await waitFor(() => expect(count("Selected · approved")).toBe("1"));
  await screen.findByTestId("review-error");
  expect(screen.queryByTestId("review-approve")).toBeNull();
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewSubmission")).toHaveLength(1);
  unmount(); runtime.destroy();
});

it("keeps the active filter and selected revision during a manual refresh, then removes revoked review actions", async () => {
  const { unmount, runtime } = await mount();
  await openQueue();
  fireEvent.click(screen.getByTestId("review-submission-12"));
  fireEvent.click(screen.getByRole("button", { name: "Selected · pending" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh batch" }));
  await waitFor(() => expect(screen.queryByText("Refreshing batch progress…")).toBeNull());
  expect(screen.getByTestId("review-submission-12").className).toContain("active");
  expect(screen.getByRole("button", { name: "Selected · pending" }).getAttribute("aria-pressed")).toBe("true");
  batch = { ...batch, items: batch.items.map((item) => ({ ...item, can_review: false })) };
  fireEvent.click(screen.getByRole("button", { name: "Refresh batch" }));
  await waitFor(() => expect(screen.queryByTestId("review-approve")).toBeNull());
  expect(screen.getByText(/review is currently unavailable/)).toBeTruthy();
  expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewSubmission")).toHaveLength(0);
  unmount(); runtime.destroy();
});

it("ignores an old project's late progress read after navigation", async () => {
  const { rerender, unmount, runtime } = await mount();
  const original = mockCallApi.getMockImplementation();
  let resolveOld;
  mockCallApi.mockImplementation((method, args) => method === "reviewBatchDetail" && args.params.pk === 4
    ? new Promise((resolve) => { resolveOld = resolve; }) : original(method, args));
  fireEvent.click(screen.getByRole("button", { name: "Refresh batch" }));
  const Provider = runtime.provider;
  rerender(<Provider><ReviewBatches projectId={5} /></Provider>);
  fireEvent.click(await screen.findByRole("button", { name: "Batch eight" }));
  await screen.findByRole("heading", { name: "Batch eight" });
  await act(async () => resolveOld(batch));
  expect(screen.getByRole("heading", { name: "Batch eight" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "Batch seven" })).toBeNull();
  unmount(); runtime.destroy();
});

it("does not leave the batch busy when a saved review refresh overlaps a successor read", async () => {
  sources = [source(11)];
  batch.items[1] = { ...batch.items[1], status: "rejected", can_review: false, successor: { id: 21, revision: 2, status: "pending" } };
  const { unmount, runtime } = await mount();
  await openQueue();
  const original = mockCallApi.getMockImplementation();
  let completeReview;
  let completeInspection;
  mockCallApi.mockImplementation((method, args) => {
    if (method === "reviewSubmission") return new Promise((resolve) => { completeReview = () => resolve(original(method, args)); });
    if (method === "submissionDetail") return new Promise((resolve) => { completeInspection = resolve; });
    return original(method, args);
  });
  fireEvent.click(screen.getByTestId("review-approve"));
  fireEvent.click(screen.getByRole("button", { name: "Inspect newer revision" }));
  await act(async () => completeReview());
  await waitFor(() => expect(count("Selected · approved")).toBe("1"));
  await act(async () => completeInspection({ id: 21, revision: 2, result_snapshot: { obsolete: true } }));
  expect(screen.getByRole("button", { name: "Refresh batch" }).disabled).toBe(false);
  expect(screen.queryByTestId("review-batch-successor")).toBeNull();
  unmount(); runtime.destroy();
});

it("distinguishes a manager's empty filtered view from review completion", async () => {
  batch = { ...batch, role: "manager" };
  const { unmount, runtime } = await mount();
  expect(screen.queryByText(/Counts and sample rate cover only/)).toBeNull();
  expect(screen.queryByRole("button", { name: "Review this sample" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Rework follow-up" }));
  expect(screen.getByText("No submissions match this filter.")).toBeTruthy();
  expect(count("Selected · pending")).toBe("2");
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("keeps rework and frozen decision counts separate, including non-approvals in %s", async (locale) => {
  const zh = locale === "zh-CN";
  batch = { ...batch, selected_count: 5, candidate_count: 6, items: [
    { submission_id: 11, revision: 1, selected: true, status: "rejected", review_reason: "Fix original", successor: { id: 21, revision: 2, status: "approved" } },
    { submission_id: 12, revision: 1, selected: true, status: "pending", can_review: false },
    { submission_id: 13, revision: 1, selected: false, status: "approved" },
    { submission_id: 14, revision: 1, selected: true, status: "superseded" },
    { submission_id: 15, revision: 1, selected: true, status: "unavailable" },
    { submission_id: 16, revision: 1, selected: true, status: "approved" },
  ] };
  const { unmount, runtime } = await mount(locale);
  expect(count(zh ? "抽中 · 已批准" : "Selected · approved")).toBe("1");
  expect(count(zh ? "抽中 · 已驳回" : "Selected · rejected")).toBe("1");
  expect(count(zh ? "抽中 · 已被替代" : "Selected · superseded")).toBe("1");
  expect(count(zh ? "抽中 · 源不可用" : "Selected · source unavailable")).toBe("1");
  expect(screen.getByText(zh ? /暂不可审核/ : /review is currently unavailable/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: zh ? "返工相关" : "Rework follow-up" }));
  const rows = screen.getAllByRole("row");
  expect(rows).toHaveLength(2);
  expect(rows[1].textContent).toContain("Fix original");
  expect(rows[1].textContent).toContain(zh ? "最新修订状态: 已批准" : "Latest revision state: Approved");
  fireEvent.click(screen.getByRole("button", { name: zh ? "抽中 · 已审核" : "Selected · reviewed" }));
  expect(screen.getAllByRole("row")).toHaveLength(3);
  fireEvent.click(screen.getByRole("button", { name: zh ? "抽中 · 待审核" : "Selected · pending" }));
  expect(screen.getAllByRole("row")).toHaveLength(2);
  expect(screen.getAllByRole("row")[1].textContent).toContain("#12");
  fireEvent.click(screen.getByRole("button", { name: zh ? "未抽中" : "Unselected" }));
  expect(screen.getAllByRole("row")[1].textContent).toContain("#13");
  // Filters do not change the denominator or summary.
  expect(count(zh ? "已抽中" : "Selected")).toBe("5");
  unmount(); runtime.destroy();
});
