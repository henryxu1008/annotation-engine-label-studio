import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ReviewBatches } from "./ReviewBatches";
import { ReviewArbitrationHandoff } from "./ReviewArbitrationHandoff";

const mockCall = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCall }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const hash = "a".repeat(64), viewer = "b".repeat(64);
const source = { batch: 7, source: 11, pending: true };
const batch = { id: 7, name: "Frozen review", role: "manager", viewer_hash: hash, plan_hash: hash,
  as_of: "now", algorithm: "classification-conflict-ranked/v1", rule: { rate: 100, seed: "fixed" }, candidate_count: 1, selected_count: 1,
  items: [{ submission_id: 11, revision: 1, selected: true, status: "pending", can_review: false, priority_reason: "frozen_conflict" }] };
const history = () => ({ schema: "review-arbitration-handoffs/v1", review_batch_id: 7, submission_id: 11,
  viewer_hash: viewer, count: 0, next: null, previous: null, results: [] });
const respond = async (m) => {
  if (m === "reviewBatches") return { results: { batches: [{ id: 7, name: batch.name }] } };
  if (m === "reviewBatchCapability") return { enabled: true, can_create: false, can_read_priority: true, can_read_handoffs: true, viewer_hash: hash };
  if (m === "reviewBatchDetail") return batch;
  if (m === "classificationArbitrationCapability") return { enabled: true, viewer_hash: viewer, can_read: true, can_create: true };
  if (m === "reviewHandoffs") return history();
  throw new Error(m);
};
const mount = (children, locale = "en-US") => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider;
  return { ...render(<Provider>{children}</Provider>), runtime };
};
beforeEach(() => mockCall.mockReset().mockImplementation(respond));

it.each(["en-US", "zh-CN"])("returns to the original frozen batch, filter and focus after fresh authorization in %s", async (locale) => {
  const { unmount, runtime } = mount(<ReviewBatches projectId={4} />, locale), zh = locale === "zh-CN";
  fireEvent.click(await screen.findByRole("button", { name: batch.name }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "抽中 · 待审核" : "Selected · pending" }));
  fireEvent.click(screen.getByRole("button", { name: zh ? "审核仲裁交接" : "Review arbitration handoff" }));
  await screen.findByTestId("review-handoff");
  await screen.findByRole("button", { name: zh ? "准备交接" : "Prepare handoff" });
  expect(screen.queryByText(batch.name)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: zh ? "返回审核批次" : "Back to review batch" }));
  await screen.findByRole("heading", { name: batch.name });
  await waitFor(() => expect(screen.getByRole("button", { name: zh ? "审核仲裁交接" : "Review arbitration handoff" })).toBe(document.activeElement));
  expect(screen.getByRole("button", { name: zh ? "抽中 · 待审核" : "Selected · pending" }).getAttribute("aria-pressed")).toBe("true");
  expect(mockCall.mock.calls.filter(([m]) => m === "reviewBatchDetail")).toHaveLength(2);
  expect(mockCall.mock.calls.filter(([m]) => m === "reviewBatchCapability")).toHaveLength(3);
  unmount(); runtime.destroy();
});

it("does not retain source entries after fresh access is revoked on return", async () => {
  const { unmount, runtime } = mount(<ReviewBatches projectId={4} />);
  fireEvent.click(await screen.findByRole("button", { name: batch.name }));
  fireEvent.click(await screen.findByRole("button", { name: "Review arbitration handoff" }));
  await screen.findByRole("button", { name: "Prepare handoff" });
  mockCall.mockResolvedValue({ error: true });
  fireEvent.click(screen.getByRole("button", { name: "Back to review batch" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Review arbitration handoff" })).toBeNull();
  expect(screen.queryByText(batch.name)).toBeNull();
  unmount(); runtime.destroy();
});

it.each(["viewer", "source", "denial"])("rejects a handoff history with a changed %s", async (kind) => {
  mockCall.mockImplementation((m, o) => m === "reviewHandoffs" ? Promise.resolve(kind === "denial" ? { error: true }
    : { ...history(), ...(kind === "viewer" ? { viewer_hash: "c".repeat(64) } : { submission_id: 12 }) }) : respond(m, o));
  const { unmount, runtime } = mount(<ReviewArbitrationHandoff projectId={4} source={source} onBack={() => {}} />);
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Prepare handoff" })).toBeNull();
  unmount(); runtime.destroy();
});

it("fences a history response that arrives after blur", async () => {
  let finish;
  mockCall.mockImplementation((m, o) => m === "reviewHandoffs" ? new Promise((r) => { finish = r; }) : respond(m, o));
  const { unmount, runtime } = mount(<ReviewArbitrationHandoff projectId={4} source={source} onBack={() => {}} />);
  await waitFor(() => expect(finish).toBeDefined()); fireEvent.blur(window);
  await act(async () => finish(history()));
  expect(screen.queryByRole("button", { name: "Prepare handoff" })).toBeNull();
  unmount(); runtime.destroy();
});
