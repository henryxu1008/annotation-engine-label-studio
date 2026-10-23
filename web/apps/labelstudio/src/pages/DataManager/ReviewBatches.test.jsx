import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
