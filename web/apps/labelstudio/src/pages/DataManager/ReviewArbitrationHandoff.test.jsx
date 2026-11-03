import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Profiler } from "react";
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
const source = { batch: 7, source: 11, pending: true, kind: "classification" };
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

it.each([undefined, "unknown", "__proto__", "toString"])("rejects an unsupported handoff kind %s without a classification fallback", async (kind) => {
  const view = mount(<ReviewArbitrationHandoff projectId={4} source={{ ...source, kind }} onBack={() => {}} />);
  await screen.findByRole("alert");
  expect(mockCall).not.toHaveBeenCalled();
  view.unmount(); view.runtime.destroy();
});

const focusFlows = [
  ["classification", "reviewHandoffs", "classificationArbitrationCapability", "review-arbitration-handoffs/v1"],
  ["ner", "nerReviewHandoffs", "nerArbitrationCapability", "review-ner-arbitration-handoff/v1"],
  ["field", "fieldReviewHandoffs", "fieldArbitrationCapability", "review-field-arbitration-handoff/v1"],
];
const focusHistory = (kind, schema) => ({
  ...history(), schema, kind, project_id: 4,
  processing_cost: { validation_passes: 2, hydration_passes: 0, calculation_passes: 0,
    per_pass: { normalized_values: 0, field_equalities: 0 },
    total: { normalized_values: 0, field_equalities: 0 }, scorer_calls: 0 },
});

describe.each(focusFlows)("%s handoff focus", (kind, listMethod, capabilityMethod, schema) => {
  const prepareHistory = () => {
    let finish;
    mockCall.mockImplementation((method) => {
      if (method === listMethod) return new Promise((resolve) => { finish = resolve; });
      if (method === capabilityMethod) return Promise.resolve({ enabled: true, can_read: true, can_create: true,
        actor_id: 1, viewer_hash: viewer, schema: `${kind}-arbitrations/v1` });
      return respond(method);
    });
    return (value = focusHistory(kind, schema)) => finish(value);
  };

  it.each(["en-US", "zh-CN"])("does not steal focus from the newly ready button in %s", async (locale) => {
    const finish = prepareHistory();
    const trace = [];
    const record = (event) => trace.push(event.target.tagName);
    document.addEventListener("focusin", record);
    let button;
    // Profiler observes the committed DOM before passive effects. Reproduce the
    // readiness/activation interleaving without timers or mocking React effects.
    const view = mount(<Profiler id="handoff" onRender={() => {
      const ready = document.querySelector('[data-testid="handoff-start"]');
      if (ready && !button) { button = ready; button.focus(); }
    }}><ReviewArbitrationHandoff projectId={4} source={{ ...source, kind }} onBack={() => {}} /></Profiler>, locale);
    try {
      expect(screen.getByRole("heading")).toBe(document.activeElement);
      await act(async () => finish());
      expect(button).toBeDefined();
      expect(trace).toEqual(["H3", "BUTTON"]);
      expect(document.activeElement).toBe(button);
    } finally {
      document.removeEventListener("focusin", record);
      view.unmount(); view.runtime.destroy();
    }
  });

  it.each(["success", "denied", "blur"])("preserves deliberate focus after a %s read", async (outcome) => {
    const finish = prepareHistory();
    const view = mount(<><button type="button">Outside handoff</button>
      <ReviewArbitrationHandoff projectId={4} source={{ ...source, kind }} onBack={() => {}} /></>);
    try {
      const outside = screen.getByRole("button", { name: "Outside handoff" });
      outside.focus();
      if (outcome === "blur") fireEvent.blur(window);
      await act(async () => finish(outcome === "denied" ? { error: true } : focusHistory(kind, schema)));
      expect(document.activeElement).toBe(outside);
      expect(screen.queryByTestId("handoff-start") !== null).toBe(outcome === "success");
    } finally { view.unmount(); view.runtime.destroy(); }
  });

  it("does not move focus when a manual refresh finishes", async () => {
    const finish = prepareHistory();
    const view = mount(<><button type="button">Outside handoff</button>
      <ReviewArbitrationHandoff projectId={4} source={{ ...source, kind }} onBack={() => {}} /></>);
    try {
      await act(async () => finish());
      fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
      const outside = screen.getByRole("button", { name: "Outside handoff" });
      outside.focus();
      await act(async () => finish());
      expect(document.activeElement).toBe(outside);
      expect(screen.getByTestId("handoff-start")).toBeDefined();
    } finally { view.unmount(); view.runtime.destroy(); }
  });

  it("focuses the heading on entry and return before passive effects, while reloading", async () => {
    const finish = prepareHistory();
    const focusedAtCommit = [];
    const view = mount(<Profiler id="handoff" onRender={() => {
      const historyView = document.querySelector('[data-testid="review-handoff"]');
      if (historyView) focusedAtCommit.push(historyView.querySelector("h3") === document.activeElement);
    }}><ReviewArbitrationHandoff projectId={4} source={{ ...source, kind }} onBack={() => {}} /></Profiler>);
    try {
      expect(focusedAtCommit[0]).toBe(true);
      await act(async () => finish());
      fireEvent.click(screen.getByTestId("handoff-start"));
      const back = await screen.findByRole("button", { name: "Back to handoff history" });
      await waitFor(() => expect(back.disabled).toBe(false));
      const beforeReturn = focusedAtCommit.length;
      fireEvent.click(back);
      expect(focusedAtCommit[beforeReturn]).toBe(true);
      expect(screen.getByRole("heading")).toBe(document.activeElement);
      expect(screen.getByTestId("review-handoff").getAttribute("aria-busy")).toBe("true");
      await act(async () => finish());
    } finally { view.unmount(); view.runtime.destroy(); }
  });
});
